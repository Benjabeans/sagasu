"""Private command-line executor that runs inside one session container."""

from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path
from typing import (
    Any,
    BinaryIO,
    Callable,
    Mapping,
    NoReturn,
    Sequence,
    TextIO,
    cast,
)

from sagasu.cdp.dom import stream_active_dom
from sagasu.cdp.insert_text import insert_text_active_page
from sagasu.cdp.locate import ElementLocation, locate_active_element
from sagasu.cdp.navigate import navigate_active_page
from sagasu.cli.action_sequence import (
    ActionSequenceConfig,
    SequenceExecution,
    parse_action_sequence,
    prepare_cursor_backends,
    run_action_sequence,
    validate_sequence_coordinates,
)
from sagasu.protocol import SagasuError, success, write_json
from sagasu.sessions import human_control
from sagasu.sessions.activity import agent_activity, paths_for_lock
from sagasu.sessions.locking import LOCK_PATH, session_lock
from sagasu.xcontrol.capture import stream_png
from sagasu.xcontrol.cursor import create_backend, normalize_button
from sagasu.xcontrol.display import (
    DisplaySize,
    PointerPosition,
    get_display_size,
    get_pointer_position,
    validate_coordinate,
)


# The largest validator-accepted sequence (100 maximum-sized text inserts,
# including worst-case JSON escaping) is under 40 MiB. Keep the private stdin
# protocol finite while leaving headroom for its object fields.
MAX_SEQUENCE_INPUT_BYTES = 64 * 1024 * 1024
SEQUENCE_INPUT_CHUNK_BYTES = 64 * 1024


class ProtocolArgumentParser(argparse.ArgumentParser):
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        # This is a private machine-to-machine interface. Default argparse
        # help writes human-readable text to stdout and exits before main can
        # serialize the failure, so keep every parser in the tree JSON-only.
        kwargs["add_help"] = False
        super().__init__(*args, **kwargs)

    def error(self, message: str) -> NoReturn:
        raise SagasuError(
            "invalid_arguments",
            message,
            exit_status=2,
        )


def build_parser() -> argparse.ArgumentParser:
    parser = ProtocolArgumentParser(prog="sagasu-session-exec")
    commands = parser.add_subparsers(dest="command", required=True)

    commands.add_parser("display", help="query display and pointer geometry")

    screenshot = commands.add_parser(
        "screenshot", help="write a full-display PNG to stdout"
    )
    screenshot.add_argument(
        "--no-pointer",
        action="store_true",
        help="exclude the X pointer from the image",
    )

    commands.add_parser(
        "dom", help="write the active page's live HTML DOM to stdout"
    )

    locate = commands.add_parser(
        "locate", help="locate a visible element in X-display coordinates"
    )
    locate.add_argument("selector")

    navigate = commands.add_parser(
        "navigate", help="navigate the active page through CDP"
    )
    navigate.add_argument("url")

    insert_text = commands.add_parser(
        "insert-text",
        help="insert text into the focused page element through CDP",
    )
    insert_text.add_argument("text")

    sequence = commands.add_parser(
        "sequence",
        help="apply bounded input actions and stream a final screenshot",
    )
    sequence.add_argument("--settle-ms", type=int)
    sequence.add_argument("--no-pointer", action="store_true")

    cursor = commands.add_parser("cursor", help="inspect or mutate the cursor")
    cursor_commands = cursor.add_subparsers(
        dest="cursor_command", required=True
    )
    cursor_commands.add_parser("position", help="query pointer position")

    move = cursor_commands.add_parser("move", help="move the pointer")
    move.add_argument("x", type=int)
    move.add_argument("y", type=int)
    _add_movement_options(move)

    click = cursor_commands.add_parser("click", help="move and click")
    click.add_argument("coordinates", nargs="*", type=int, metavar="COORD")
    click.add_argument(
        "--current",
        action="store_true",
        help="click the current location (debugging only)",
    )
    click.add_argument("--button", default="left")
    click.add_argument("--count", type=int, default=1)
    click.add_argument("--hold-ms", type=int, default=0)
    _add_backend(click)

    drag = cursor_commands.add_parser("drag", help="move and drag")
    drag.add_argument("coordinates", nargs="*", type=int, metavar="COORD")
    drag.add_argument(
        "--current",
        action="store_true",
        help="drag from the current location (debugging only)",
    )
    _add_movement_options(drag)

    scroll = cursor_commands.add_parser("scroll", help="move and scroll")
    scroll.add_argument("coordinates", nargs="*", type=int, metavar="COORD")
    scroll.add_argument(
        "--current",
        action="store_true",
        help="scroll at the current location (debugging only)",
    )
    scroll.add_argument("--steps", type=int, required=True)
    _add_backend(scroll)

    human = commands.add_parser(
        "human", help=argparse.SUPPRESS
    )
    human_commands = human.add_subparsers(dest="human_command", required=True)
    human_commands.add_parser("pause", help=argparse.SUPPRESS)
    human_commands.add_parser("resume", help=argparse.SUPPRESS)
    return parser


def _add_backend(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--backend",
        choices=("humancursor", "xdotool"),
        default="humancursor",
    )


def _add_movement_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--duration-ms", type=int)
    parser.add_argument("--steady", action="store_true")
    _add_backend(parser)


def _duration_seconds(value: int | None) -> float | None:
    if value is None:
        return None
    if value < 0:
        raise SagasuError(
            "invalid_arguments",
            "--duration-ms must be zero or greater",
            {"duration_ms": value},
            exit_status=2,
        )
    return value / 1000


def _positive_count(value: int) -> int:
    if value <= 0:
        raise SagasuError(
            "invalid_arguments",
            "--count must be greater than zero",
            {"count": value},
            exit_status=2,
        )
    return value


def _hold_seconds(value: int) -> float:
    if value < 0:
        raise SagasuError(
            "invalid_arguments",
            "--hold-ms must be zero or greater",
            {"hold_ms": value},
            exit_status=2,
        )
    return value / 1000


def _result(
    operation: str,
    backend: str,
    display: DisplaySize,
    pointer: PointerPosition | None = None,
    **extra: object,
) -> dict[str, object]:
    pointer = pointer or get_pointer_position()
    return success(
        operation,
        backend=backend,
        width=display.width,
        height=display.height,
        pointer_x=pointer.x,
        pointer_y=pointer.y,
        **extra,
    )


def execute(
    arguments: argparse.Namespace,
    *,
    binary_stdin: BinaryIO | None = None,
    text_stdout: TextIO | None = None,
    binary_stdout: BinaryIO | None = None,
    metadata_stream: TextIO | None = None,
    lock_path: Path | str = LOCK_PATH,
    pause_path: Path | str = human_control.PAUSE_PATH,
    activity_path: Path | str | None = None,
    idle_gate_path: Path | str | None = None,
    environ: Mapping[str, str] | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> None:
    default_activity_path, default_idle_gate_path = paths_for_lock(lock_path)
    with agent_activity(
        activity_path=activity_path or default_activity_path,
        gate_path=idle_gate_path or default_idle_gate_path,
    ):
        _execute_command(
            arguments,
            binary_stdin=binary_stdin,
            text_stdout=text_stdout,
            binary_stdout=binary_stdout,
            metadata_stream=metadata_stream,
            lock_path=lock_path,
            pause_path=pause_path,
            environ=os.environ if environ is None else environ,
            sleep=sleep,
        )


def _execute_command(
    arguments: argparse.Namespace,
    *,
    binary_stdin: BinaryIO | None,
    text_stdout: TextIO | None,
    binary_stdout: BinaryIO | None,
    metadata_stream: TextIO | None,
    lock_path: Path | str,
    pause_path: Path | str,
    environ: Mapping[str, str],
    sleep: Callable[[float], None],
) -> None:
    # typeshed types sys.stdout/stderr as "TextIO | Any" because they can be
    # detached; cast so the None-default parameters narrow properly.
    if text_stdout is None:
        text_stdout = cast(TextIO, sys.stdout)
    if metadata_stream is None:
        metadata_stream = cast(TextIO, sys.stderr)
    if arguments.command == "sequence":
        if binary_stdin is None:
            binary_stdin = cast(BinaryIO, sys.stdin.buffer)
        if binary_stdout is None:
            binary_stdout = cast(BinaryIO, sys.stdout.buffer)
        _execute_sequence(
            arguments,
            binary_stdin=binary_stdin,
            binary_stdout=binary_stdout,
            metadata_stream=metadata_stream,
            lock_path=lock_path,
            pause_path=pause_path,
            environ=environ,
            sleep=sleep,
        )
        return

    if arguments.command == "screenshot":
        if binary_stdout is None:
            binary_stdout = cast(BinaryIO, sys.stdout.buffer)
        with session_lock(exclusive=False, path=lock_path):
            stream_png(
                binary_stdout,
                include_pointer=not arguments.no_pointer,
            )
        return

    if arguments.command == "dom":
        if binary_stdout is None:
            binary_stdout = cast(BinaryIO, sys.stdout.buffer)
        with session_lock(exclusive=False, path=lock_path):
            snapshot = stream_active_dom(binary_stdout)
            display = get_display_size()
            pointer = get_pointer_position()
        write_json(
            _result(
                "dom.fetch",
                "cdp",
                display,
                pointer,
                target_id=snapshot.target_id,
                title=snapshot.title,
                url=snapshot.url,
                bytes=snapshot.byte_count,
            ),
            metadata_stream,
        )
        return

    if arguments.command == "display":
        with session_lock(exclusive=False, path=lock_path):
            display = get_display_size()
            pointer = get_pointer_position()
        write_json(_result("display", "xdotool", display, pointer), text_stdout)
        return

    if arguments.command == "locate":
        with session_lock(exclusive=False, path=lock_path):
            display = get_display_size()
            location = locate_active_element(
                arguments.selector,
                display_width=display.width,
                display_height=display.height,
            )
            pointer = get_pointer_position()
        write_json(
            _result(
                "element.locate",
                "cdp",
                display,
                pointer,
                **_location_metadata(location),
            ),
            text_stdout,
        )
        return

    if arguments.command in ("navigate", "insert-text"):
        payload = _execute_cdp_mutation(
            arguments,
            lock_path=lock_path,
            pause_path=pause_path,
        )
        write_json(payload, text_stdout)
        return

    if arguments.command == "human":
        with session_lock(exclusive=True, path=lock_path):
            if arguments.human_command == "pause":
                human_control.pause(pause_path)
                operation = "human.pause"
            else:
                human_control.resume(pause_path)
                operation = "human.resume"
            display = get_display_size()
            pointer = get_pointer_position()
        write_json(_result(operation, "none", display, pointer), text_stdout)
        return

    assert arguments.command == "cursor"
    if arguments.cursor_command == "position":
        with session_lock(exclusive=False, path=lock_path):
            display = get_display_size()
            pointer = get_pointer_position()
        write_json(
            _result("cursor.position", "xdotool", display, pointer),
            text_stdout,
        )
        return

    payload = _execute_mutation(
        arguments,
        lock_path=lock_path,
        pause_path=pause_path,
    )
    write_json(payload, text_stdout)


def _execute_sequence(
    arguments: argparse.Namespace,
    *,
    binary_stdin: BinaryIO,
    binary_stdout: BinaryIO,
    metadata_stream: TextIO,
    lock_path: Path | str,
    pause_path: Path | str,
    environ: Mapping[str, str],
    sleep: Callable[[float], None],
) -> None:
    config = ActionSequenceConfig.from_environ(environ)
    actions = parse_action_sequence(
        _read_sequence_input(binary_stdin),
        max_actions=config.max_actions,
    )
    settle_ms = config.effective_settle_ms(arguments.settle_ms)

    human_control.require_agent_control(pause_path)
    backends = prepare_cursor_backends(
        actions,
        backend_factory=create_backend,
    )

    with session_lock(exclusive=True, path=lock_path):
        human_control.require_agent_control(pause_path)
        display = get_display_size()
        validate_sequence_coordinates(actions, display)
        execution = run_action_sequence(
            actions,
            display,
            backend_factory=backends.__getitem__,
        )
        if settle_ms:
            sleep(settle_ms / 1000)
        try:
            stream_png(
                binary_stdout,
                include_pointer=not arguments.no_pointer,
            )
        except SagasuError as error:
            _raise_sequence_observation_failure(
                error,
                execution=execution,
                action_count=len(actions),
                display=display,
                settle_ms=settle_ms,
                pointer_included=not arguments.no_pointer,
                stage="screenshot",
            )
        try:
            pointer = get_pointer_position()
        except SagasuError as error:
            _raise_sequence_observation_failure(
                error,
                execution=execution,
                action_count=len(actions),
                display=display,
                settle_ms=settle_ms,
                pointer_included=not arguments.no_pointer,
                stage="pointer",
            )

    extra: dict[str, object] = {
        "completed": execution.completed,
        "action_count": len(actions),
        "actions_completed": len(execution.results),
        "settle_ms": settle_ms,
        "pointer_included": not arguments.no_pointer,
        "results": list(execution.results),
    }
    if execution.failure is not None:
        extra["failed_index"] = execution.failed_index
        extra["failure"] = execution.failure.as_dict()["error"]
    write_json(
        _result(
            "actions.sequence",
            "mixed",
            display,
            pointer,
            **extra,
        ),
        metadata_stream,
    )


def _raise_sequence_observation_failure(
    error: SagasuError,
    *,
    execution: SequenceExecution,
    action_count: int,
    display: DisplaySize,
    settle_ms: int,
    pointer_included: bool,
    stage: str,
) -> NoReturn:
    """Preserve authoritative mutation state when final observation fails."""

    state: dict[str, object] = {
        "completed": execution.completed,
        "action_count": action_count,
        "actions_completed": len(execution.results),
        "display": {"width": display.width, "height": display.height},
        "results": list(execution.results),
        "settle_ms": settle_ms,
        "settle_completed": True,
        "pointer_included": pointer_included,
        "screenshot_captured": stage == "pointer",
        "pointer_observed": False,
        "observation_stage": stage,
    }
    if execution.failure is not None:
        state["failed_index"] = execution.failed_index
        state["failure"] = execution.failure.as_dict()["error"]

    details = dict(error.details)
    details["sequence_state"] = state
    raise SagasuError(
        error.code,
        error.message,
        details,
        exit_status=error.exit_status,
    ) from error


def _read_sequence_input(stream: BinaryIO) -> str:
    """Read one EOF-delimited, bounded UTF-8 action document from stdin."""

    chunks: list[bytes] = []
    remaining = MAX_SEQUENCE_INPUT_BYTES + 1
    try:
        while remaining:
            chunk = stream.read(min(SEQUENCE_INPUT_CHUNK_BYTES, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
    except OSError as exc:
        raise SagasuError(
            "invalid_arguments",
            "The action sequence could not be read from stdin",
            {"reason": str(exc)},
            exit_status=2,
        ) from exc

    data = b"".join(chunks)
    if not data:
        raise SagasuError(
            "invalid_arguments",
            "The action sequence is required on stdin",
            exit_status=2,
        )
    if len(data) > MAX_SEQUENCE_INPUT_BYTES:
        raise SagasuError(
            "invalid_arguments",
            "The action sequence supplied on stdin is too large",
            {
                "bytes_at_least": len(data),
                "max_bytes": MAX_SEQUENCE_INPUT_BYTES,
            },
            exit_status=2,
        )
    try:
        return data.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise SagasuError(
            "invalid_arguments",
            "The action sequence supplied on stdin is not valid UTF-8",
            {
                "byte_offset": exc.start,
                "reason": exc.reason,
            },
            exit_status=2,
        ) from exc


def _execute_cdp_mutation(
    arguments: argparse.Namespace,
    *,
    lock_path: Path | str,
    pause_path: Path | str,
) -> dict[str, object]:
    with session_lock(exclusive=True, path=lock_path):
        human_control.require_agent_control(pause_path)
        if arguments.command == "navigate":
            result = navigate_active_page(arguments.url)
            extra: dict[str, object] = {
                "target_id": result.target_id,
                "requested_url": result.requested_url,
                "frame_id": result.frame_id,
                "loader_id": result.loader_id,
                "is_download": result.is_download,
            }
            operation = "page.navigate"
        else:
            result = insert_text_active_page(arguments.text)
            extra = {
                "target_id": result.target_id,
                "title": result.title,
                "url": result.url,
                "characters": result.character_count,
                "bytes": result.byte_count,
            }
            operation = "text.insert"
        display = get_display_size()
        pointer = get_pointer_position()
    return _result(operation, "cdp", display, pointer, **extra)


def _location_metadata(location: ElementLocation) -> dict[str, object]:
    return {
        "target_id": location.target_id,
        "title": location.title,
        "url": location.url,
        "selector": location.selector,
        "node_id": location.node_id,
        "screen": {
            "x": location.screen_x,
            "y": location.screen_y,
        },
        "viewport": {
            "point": {
                "x": location.viewport_x,
                "y": location.viewport_y,
            },
            "width": location.viewport_width,
            "height": location.viewport_height,
            "quad": list(location.viewport_quad),
            "visible_polygon": [
                {"x": point[0], "y": point[1]}
                for point in location.visible_polygon
            ],
        },
        "mapping": {
            "viewport_origin": {
                "x": location.viewport_origin_x,
                "y": location.viewport_origin_y,
            },
            "scale": {
                "x": location.scale_x,
                "y": location.scale_y,
            },
            "browser_window": {
                "left": location.window_left,
                "top": location.window_top,
                "width": location.window_width,
                "height": location.window_height,
            },
        },
    }


def _execute_mutation(
    arguments: argparse.Namespace,
    *,
    lock_path: Path | str,
    pause_path: Path | str,
) -> dict[str, object]:
    command = arguments.cursor_command
    duration: float | None = None
    if hasattr(arguments, "duration_ms"):
        duration = _duration_seconds(arguments.duration_ms)
    count = 1
    hold = 0.0
    if command == "click":
        count = _positive_count(arguments.count)
        hold = _hold_seconds(arguments.hold_ms)
        normalize_button(arguments.button)
    if command == "scroll" and arguments.steps == 0:
        raise SagasuError(
            "invalid_arguments",
            "--steps cannot be zero",
            exit_status=2,
        )
    if command in ("click", "scroll"):
        expected = 0 if arguments.current else 2
        if len(arguments.coordinates) != expected:
            usage = "--current" if arguments.current else "X Y, or --current"
            raise SagasuError(
                "invalid_arguments",
                f"{command} requires {usage}",
                exit_status=2,
            )
    elif command == "drag":
        expected = 2 if arguments.current else 4
        if len(arguments.coordinates) != expected:
            usage = "--current X2 Y2" if arguments.current else "X1 Y1 X2 Y2"
            raise SagasuError(
                "invalid_arguments",
                f"drag requires {usage}",
                exit_status=2,
            )

    # Backend construction can import PIL/python-xlib and connect to X.  Keep
    # that setup out of the exclusive display critical section, but retain an
    # authoritative pause check under the lock in case human control begins
    # while the backend is being prepared.
    human_control.require_agent_control(pause_path)
    backend = create_backend(arguments.backend)

    with session_lock(exclusive=True, path=lock_path):
        human_control.require_agent_control(pause_path)
        display = get_display_size()
        current = (
            get_pointer_position()
            if getattr(arguments, "current", False)
            else PointerPosition(0, 0)
        )

        if command == "move":
            validate_coordinate(arguments.x, arguments.y, display)
            backend.move(
                arguments.x,
                arguments.y,
                duration=duration,
                steady=arguments.steady,
            )
            operation = "cursor.move"

        elif command == "click":
            x, y = _action_point(
                arguments.coordinates,
                current=current,
                use_current=arguments.current,
                action="click",
            )
            validate_coordinate(x, y, display)
            backend.click(
                x,
                y,
                button=arguments.button,
                count=count,
                hold=hold,
            )
            operation = "cursor.click"

        elif command == "drag":
            x1, y1, x2, y2 = _drag_points(
                arguments.coordinates,
                current=current,
                use_current=arguments.current,
            )
            validate_coordinate(x1, y1, display, name="start coordinate")
            validate_coordinate(x2, y2, display, name="end coordinate")
            backend.drag(
                x1,
                y1,
                x2,
                y2,
                duration=duration,
                steady=arguments.steady,
            )
            operation = "cursor.drag"

        elif command == "scroll":
            x, y = _action_point(
                arguments.coordinates,
                current=current,
                use_current=arguments.current,
                action="scroll",
            )
            validate_coordinate(x, y, display)
            backend.scroll(x, y, steps=arguments.steps)
            operation = "cursor.scroll"

        else:  # pragma: no cover - argparse guarantees the subcommand
            raise SagasuError(
                "invalid_arguments",
                f"Unknown cursor command {command!r}",
                exit_status=2,
            )

        pointer = get_pointer_position()
        return _result(operation, arguments.backend, display, pointer)


def _action_point(
    coordinates: Sequence[int],
    *,
    current: PointerPosition,
    use_current: bool,
    action: str,
) -> tuple[int, int]:
    if use_current:
        if coordinates:
            raise SagasuError(
                "invalid_arguments",
                f"{action} --current does not accept coordinates",
                exit_status=2,
            )
        return current.x, current.y
    if len(coordinates) != 2:
        raise SagasuError(
            "invalid_arguments",
            f"{action} requires X Y, or --current for debugging",
            exit_status=2,
        )
    return coordinates[0], coordinates[1]


def _drag_points(
    coordinates: Sequence[int],
    *,
    current: PointerPosition,
    use_current: bool,
) -> tuple[int, int, int, int]:
    if use_current:
        if len(coordinates) != 2:
            raise SagasuError(
                "invalid_arguments",
                "drag --current requires destination X2 Y2",
                exit_status=2,
            )
        return current.x, current.y, coordinates[0], coordinates[1]
    if len(coordinates) != 4:
        raise SagasuError(
            "invalid_arguments",
            "drag requires X1 Y1 X2 Y2, or --current X2 Y2",
            exit_status=2,
        )
    return (
        coordinates[0],
        coordinates[1],
        coordinates[2],
        coordinates[3],
    )


def main(
    argv: Sequence[str] | None = None,
    *,
    binary_stdin: BinaryIO | None = None,
) -> int:
    try:
        arguments = build_parser().parse_args(argv)
        execute(arguments, binary_stdin=binary_stdin)
        return 0
    except SagasuError as error:
        write_json(error.as_dict(), sys.stderr)
        return error.exit_status
    except BrokenPipeError:
        return 1
    except Exception as exc:  # keep the private protocol structured
        error = SagasuError(
            "input_failed",
            "The session-control executor failed unexpectedly",
            {"reason": str(exc)},
        )
        write_json(error.as_dict(), sys.stderr)
        return error.exit_status


if __name__ == "__main__":
    raise SystemExit(main())
