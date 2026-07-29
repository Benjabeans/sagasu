"""Private command-line executor that runs inside one session container."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import BinaryIO, Sequence, TextIO

from sagasu.cdp.dom import stream_active_dom
from sagasu.protocol import SagasuError, success, write_json
from sagasu.sessions import human_control
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


class ProtocolArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
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
    text_stdout: TextIO | None = None,
    binary_stdout: BinaryIO | None = None,
    metadata_stream: TextIO | None = None,
    lock_path: Path | str = LOCK_PATH,
    pause_path: Path | str = human_control.PAUSE_PATH,
) -> None:
    if text_stdout is None:
        text_stdout = sys.stdout
    if metadata_stream is None:
        metadata_stream = sys.stderr
    if arguments.command == "screenshot":
        if binary_stdout is None:
            binary_stdout = sys.stdout.buffer
        with session_lock(exclusive=False, path=lock_path):
            stream_png(
                binary_stdout,
                include_pointer=not arguments.no_pointer,
            )
        return

    if arguments.command == "dom":
        if binary_stdout is None:
            binary_stdout = sys.stdout.buffer
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
            backend = create_backend(arguments.backend)
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
            backend = create_backend(arguments.backend)
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
            backend = create_backend(arguments.backend)
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
            backend = create_backend(arguments.backend)
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


def main(argv: Sequence[str] | None = None) -> int:
    try:
        arguments = build_parser().parse_args(argv)
        execute(arguments)
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
