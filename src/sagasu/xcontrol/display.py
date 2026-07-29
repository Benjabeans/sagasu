"""Read display geometry and pointer state from the session X server."""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from typing import Callable

from sagasu.protocol import SagasuError


Runner = Callable[..., subprocess.CompletedProcess[bytes]]


@dataclass(frozen=True)
class DisplaySize:
    width: int
    height: int


@dataclass(frozen=True)
class PointerPosition:
    x: int
    y: int


def _xdotool(
    arguments: list[str],
    *,
    runner: Runner = subprocess.run,
) -> bytes:
    try:
        completed = runner(
            ["xdotool", *arguments],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
    except FileNotFoundError as exc:
        raise SagasuError(
            "input_failed",
            "xdotool is not installed in the session container",
        ) from exc
    except OSError as exc:
        raise SagasuError(
            "input_failed",
            "xdotool could not be started",
            {"reason": str(exc)},
        ) from exc
    if completed.returncode:
        raise SagasuError(
            "input_failed",
            "xdotool could not query the X display",
            {
                "reason": completed.stderr.decode(
                    "utf-8", errors="replace"
                ).strip()
            },
        )
    return completed.stdout


def get_display_size(*, runner: Runner = subprocess.run) -> DisplaySize:
    output = _xdotool(["getdisplaygeometry"], runner=runner)
    try:
        width_text, height_text = output.decode("ascii").split()
        width = int(width_text)
        height = int(height_text)
    except (UnicodeDecodeError, ValueError) as exc:
        raise SagasuError(
            "input_failed",
            "xdotool returned invalid display dimensions",
            {"output": output.decode("utf-8", errors="replace").strip()},
        ) from exc
    if width <= 0 or height <= 0:
        raise SagasuError(
            "input_failed",
            "The X display has invalid dimensions",
            {"width": width, "height": height},
        )
    return DisplaySize(width=width, height=height)


def get_pointer_position(
    *, runner: Runner = subprocess.run
) -> PointerPosition:
    output = _xdotool(["getmouselocation", "--shell"], runner=runner)
    fields: dict[str, str] = {}
    try:
        for line in output.decode("ascii").splitlines():
            key, separator, value = line.partition("=")
            if separator:
                fields[key] = value
        return PointerPosition(x=int(fields["X"]), y=int(fields["Y"]))
    except (UnicodeDecodeError, KeyError, ValueError) as exc:
        raise SagasuError(
            "input_failed",
            "xdotool returned an invalid pointer position",
            {"output": output.decode("utf-8", errors="replace").strip()},
        ) from exc


def validate_coordinate(
    x: int,
    y: int,
    display: DisplaySize,
    *,
    name: str = "coordinate",
) -> None:
    if not 0 <= x < display.width or not 0 <= y < display.height:
        raise SagasuError(
            "invalid_coordinate",
            f"{name} ({x}, {y}) is outside the display",
            {
                "coordinate": {"x": x, "y": y},
                "display": {
                    "width": display.width,
                    "height": display.height,
                },
            },
            exit_status=2,
        )
