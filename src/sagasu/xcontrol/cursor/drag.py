"""Move-and-drag implementations."""

from __future__ import annotations

from sagasu.xcontrol.cursor.types import (
    HumanCursorDriver,
    MoveCursor,
    PyAutoGUI,
    XDoToolCaller,
)


def human_drag(
    cursor: HumanCursorDriver,
    pyautogui: PyAutoGUI,
    x1: int,
    y1: int,
    x2: int,
    y2: int,
    *,
    duration: float | None,
    steady: bool,
) -> None:
    try:
        cursor.drag_and_drop(
            [x1, y1],
            [x2, y2],
            duration=duration,
            steady=steady,
        )
    except Exception:
        # HumanCursor does not guarantee release when drag_and_drop fails.
        try:
            pyautogui.mouseUp(button="left")
        except Exception:
            pass
        raise


def xdotool_drag(
    move: MoveCursor,
    call: XDoToolCaller,
    x1: int,
    y1: int,
    x2: int,
    y2: int,
    *,
    duration: float | None,
    steady: bool,
) -> None:
    segment_duration = None if duration is None else duration / 2
    move(x1, y1, duration=segment_duration, steady=steady)
    call("mousedown", "1")
    try:
        move(x2, y2, duration=segment_duration, steady=steady)
    finally:
        call("mouseup", "1")

