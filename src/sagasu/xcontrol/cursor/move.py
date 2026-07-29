"""Cursor movement implementations."""

from __future__ import annotations

import math
from typing import Callable

from sagasu.xcontrol.cursor.types import (
    HumanCursorDriver,
    XDoToolCaller,
)
from sagasu.xcontrol.display import Runner, get_pointer_position


def human_move(
    cursor: HumanCursorDriver,
    x: int,
    y: int,
    *,
    duration: float | None,
    steady: bool,
) -> None:
    cursor.move_to([x, y], duration=duration, steady=steady)


def xdotool_move(
    call: XDoToolCaller,
    x: int,
    y: int,
    *,
    duration: float | None,
    runner: Runner,
    sleep: Callable[[float], None],
    monotonic: Callable[[], float],
) -> None:
    if not duration:
        call("mousemove", "--sync", str(x), str(y))
        return
    start = get_pointer_position(runner=runner)
    frames = max(2, min(120, math.ceil(duration * 60)))
    started = monotonic()
    for frame in range(1, frames + 1):
        fraction = frame / frames
        next_x = round(start.x + (x - start.x) * fraction)
        next_y = round(start.y + (y - start.y) * fraction)
        call("mousemove", "--sync", str(next_x), str(next_y))
        deadline = started + duration * fraction
        remaining = deadline - monotonic()
        if remaining > 0:
            sleep(remaining)

