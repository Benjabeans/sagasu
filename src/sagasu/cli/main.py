"""Agent-facing ``sagasu`` command-line interface."""

from __future__ import annotations

import argparse
import sys
from typing import Sequence

from sagasu import __version__
from sagasu.cli import session
from sagasu.protocol import SagasuError, write_json
from sagasu.sessions.docker import DockerCLI


class ProtocolArgumentParser(argparse.ArgumentParser):
    _CONTAINER_TARGET = "__sagasu_explicit_container__"

    def error(self, message: str) -> None:
        raise SagasuError(
            "invalid_arguments",
            message,
            exit_status=2,
        )

    def parse_args(
        self,
        args: Sequence[str] | None = None,
        namespace: argparse.Namespace | None = None,
    ) -> argparse.Namespace:
        raw = list(sys.argv[1:] if args is None else args)
        # argparse cannot disambiguate an omitted optional positional followed
        # by a subparser when that subparser itself accepts positionals. Supply
        # an internal placeholder for the documented `--container replaces
        # SESSION` spelling, then erase it from the parsed namespace.
        if (
            len(raw) >= 4
            and raw[0] == "session"
            and raw[2] == "--container"
        ):
            raw.insert(4, self._CONTAINER_TARGET)
        parsed = super().parse_args(raw, namespace)
        if (
            getattr(parsed, "session_target", None)
            == self._CONTAINER_TARGET
        ):
            parsed.session_target = None
        return parsed


def build_parser() -> argparse.ArgumentParser:
    parser = ProtocolArgumentParser(prog="sagasu")
    parser.add_argument("--version", action="version", version=__version__)
    commands = parser.add_subparsers(dest="command", required=True)

    session = commands.add_parser("session", help="control a browser session")
    session_commands = session.add_subparsers(
        dest="session_command", required=True
    )

    display = session_commands.add_parser(
        "display", help="query full-display geometry and pointer position"
    )
    _add_target(display)

    screenshot = session_commands.add_parser(
        "screenshot", help="capture a full-display PNG"
    )
    _add_target(screenshot)
    screenshot.add_argument("--out", required=True)
    screenshot.add_argument("--no-pointer", action="store_true")
    screenshot.add_argument("--overwrite", action="store_true")

    dom = session_commands.add_parser(
        "dom", help="save the active page's live HTML DOM"
    )
    _add_target(dom)
    dom.add_argument("--out", required=True)
    dom.add_argument("--overwrite", action="store_true")

    cursor = session_commands.add_parser(
        "cursor", help="inspect or drive the real X cursor"
    )
    _add_target(cursor)
    cursor_commands = cursor.add_subparsers(
        dest="cursor_command", required=True
    )

    cursor_commands.add_parser("position", help="report pointer position")

    move = cursor_commands.add_parser("move", help="move to X Y")
    move.add_argument("x", type=int)
    move.add_argument("y", type=int)
    _add_movement_options(move)

    click = cursor_commands.add_parser("click", help="move to X Y and click")
    click.add_argument("coordinates", nargs="*", type=int, metavar="COORD")
    click.add_argument("--current", action="store_true")
    click.add_argument(
        "--button",
        choices=("left", "middle", "right", "1", "2", "3"),
        default="left",
    )
    click.add_argument("--count", type=int, default=1)
    click.add_argument("--hold-ms", type=int, default=0)
    _add_backend(click)

    drag = cursor_commands.add_parser(
        "drag", help="drag from X1 Y1 to X2 Y2"
    )
    drag.add_argument("coordinates", nargs="*", type=int, metavar="COORD")
    drag.add_argument("--current", action="store_true")
    _add_movement_options(drag)

    scroll = cursor_commands.add_parser(
        "scroll", help="move to X Y and scroll"
    )
    scroll.add_argument("coordinates", nargs="*", type=int, metavar="COORD")
    scroll.add_argument("--current", action="store_true")
    scroll.add_argument("--steps", type=int, required=True)
    _add_backend(scroll)
    return parser


def _add_target(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("session_target", nargs="?", metavar="SESSION")
    parser.add_argument(
        "--container",
        help="debugging override: address a running Sagasu container by name",
    )


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


def main(argv: Sequence[str] | None = None) -> int:
    try:
        arguments = build_parser().parse_args(argv)
        if arguments.command != "session":  # pragma: no cover
            raise SagasuError(
                "invalid_arguments",
                f"Unsupported command {arguments.command!r}",
                exit_status=2,
            )
        payload = session.run(arguments, DockerCLI())
        write_json(payload, sys.stdout)
        return 0
    except SagasuError as error:
        write_json(error.as_dict(), sys.stderr)
        return error.exit_status
    except BrokenPipeError:
        return 1
    except Exception as exc:
        error = SagasuError(
            "internal_error",
            "Sagasu failed unexpectedly",
            {"reason": str(exc)},
        )
        write_json(error.as_dict(), sys.stderr)
        return error.exit_status


if __name__ == "__main__":
    raise SystemExit(main())
