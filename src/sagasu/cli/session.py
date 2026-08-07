"""Host-side handlers for ``sagasu session`` commands."""

from __future__ import annotations

import argparse
from typing import Any

from sagasu.cdp.insert_text import validate_insert_text
from sagasu.cdp.locate import validate_selector
from sagasu.cdp.navigate import validate_navigation_url
from sagasu.cli.action_sequence import (
    encode_action_sequence,
    parse_action_sequence,
    validate_settle_ms,
)
from sagasu.protocol import SagasuError
from sagasu.sessions.artifacts import (
    save_action_sequence_screenshot,
    save_dom,
    save_screenshot,
)
from sagasu.sessions.docker import DockerCLI
from sagasu.sessions.executor import SessionExecutor
from sagasu.sessions.resolver import resolve_session


def run(arguments: argparse.Namespace, docker: DockerCLI) -> dict[str, Any]:
    runtime_arguments: list[str] | None = None
    if arguments.session_command not in ("screenshot", "dom"):
        # Validate the complete action shape before contacting Docker.
        runtime_arguments = _runtime_arguments(arguments)
    resolved = resolve_session(
        docker,
        session_id=arguments.session_target,
        container=arguments.container,
    )
    executor = SessionExecutor(docker, resolved)
    if arguments.session_command == "screenshot":
        return save_screenshot(
            executor,
            arguments.out,
            include_pointer=not arguments.no_pointer,
            overwrite=arguments.overwrite,
        )
    if arguments.session_command == "dom":
        return save_dom(
            executor,
            arguments.out,
            overwrite=arguments.overwrite,
        )
    assert runtime_arguments is not None
    if arguments.session_command == "sequence":
        return save_action_sequence_screenshot(
            executor,
            arguments.out,
            executor_arguments=runtime_arguments,
            overwrite=arguments.overwrite,
        )
    return executor.invoke(runtime_arguments)


def _runtime_arguments(arguments: argparse.Namespace) -> list[str]:
    command = arguments.session_command
    if command == "display":
        return ["display"]
    if command == "locate":
        validate_selector(arguments.selector)
        return ["locate", "--", arguments.selector]
    if command == "navigate":
        validate_navigation_url(arguments.url)
        return ["navigate", "--", arguments.url]
    if command == "insert-text":
        validate_insert_text(arguments.text)
        return ["insert-text", "--", arguments.text]
    if command == "sequence":
        actions = parse_action_sequence(arguments.actions_json)
        runtime = [
            "sequence",
            "--actions-json",
            encode_action_sequence(actions),
        ]
        if arguments.settle_ms is not None:
            runtime.extend(
                ["--settle-ms", str(validate_settle_ms(arguments.settle_ms))]
            )
        if arguments.no_pointer:
            runtime.append("--no-pointer")
        return runtime
    if command != "cursor":  # pragma: no cover - parser constrains this
        raise SagasuError(
            "invalid_arguments",
            f"Unsupported session command {command!r}",
            exit_status=2,
        )

    operation = arguments.cursor_command
    runtime = ["cursor", operation]
    if operation == "position":
        return runtime

    if operation == "move":
        runtime.extend([str(arguments.x), str(arguments.y)])
        _append_movement_options(runtime, arguments)
        return runtime

    coordinates = list(arguments.coordinates)
    if operation in ("click", "scroll"):
        _validate_action_coordinates(
            coordinates,
            current=arguments.current,
            operation=operation,
        )
    elif operation == "drag":
        expected = 2 if arguments.current else 4
        if len(coordinates) != expected:
            usage = "--current X2 Y2" if arguments.current else "X1 Y1 X2 Y2"
            raise SagasuError(
                "invalid_arguments",
                f"drag requires {usage}",
                exit_status=2,
            )
    runtime.extend(str(value) for value in coordinates)
    if arguments.current:
        runtime.append("--current")

    if operation == "click":
        if arguments.count <= 0:
            raise SagasuError(
                "invalid_arguments",
                "--count must be greater than zero",
                {"count": arguments.count},
                exit_status=2,
            )
        if arguments.hold_ms < 0:
            raise SagasuError(
                "invalid_arguments",
                "--hold-ms must be zero or greater",
                {"hold_ms": arguments.hold_ms},
                exit_status=2,
            )
        runtime.extend(
            [
                "--button",
                arguments.button,
                "--count",
                str(arguments.count),
                "--hold-ms",
                str(arguments.hold_ms),
                "--backend",
                arguments.backend,
            ]
        )
    elif operation == "drag":
        _append_movement_options(runtime, arguments)
    elif operation == "scroll":
        if arguments.steps == 0:
            raise SagasuError(
                "invalid_arguments",
                "--steps cannot be zero",
                exit_status=2,
            )
        runtime.extend(
            [
                "--steps",
                str(arguments.steps),
                "--backend",
                arguments.backend,
            ]
        )
    return runtime


def _validate_action_coordinates(
    coordinates: list[int],
    *,
    current: bool,
    operation: str,
) -> None:
    expected = 0 if current else 2
    if len(coordinates) != expected:
        usage = "--current" if current else "X Y, or --current"
        raise SagasuError(
            "invalid_arguments",
            f"{operation} requires {usage}",
            exit_status=2,
        )


def _append_movement_options(
    runtime: list[str], arguments: argparse.Namespace
) -> None:
    if arguments.duration_ms is not None:
        if arguments.duration_ms < 0:
            raise SagasuError(
                "invalid_arguments",
                "--duration-ms must be zero or greater",
                {"duration_ms": arguments.duration_ms},
                exit_status=2,
            )
        runtime.extend(["--duration-ms", str(arguments.duration_ms)])
    if arguments.steady:
        runtime.append("--steady")
    runtime.extend(["--backend", arguments.backend])
