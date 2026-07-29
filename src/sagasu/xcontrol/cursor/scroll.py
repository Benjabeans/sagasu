"""Move-and-scroll implementations."""

from __future__ import annotations

from sagasu.xcontrol.cursor.types import (
    HumanCursorDriver,
    MoveCursor,
    PyAutoGUI,
    XDoToolCaller,
)


def human_scroll(
    cursor: HumanCursorDriver,
    pyautogui: PyAutoGUI,
    x: int,
    y: int,
    *,
    steps: int,
) -> None:
    cursor.move_to([x, y])
    # PyAutoGUI and xdotool agree: positive is wheel-up.
    pyautogui.scroll(steps)


def xdotool_scroll(
    move: MoveCursor,
    call: XDoToolCaller,
    x: int,
    y: int,
    *,
    steps: int,
) -> None:
    move(x, y, duration=None, steady=False)
    button = "4" if steps > 0 else "5"
    call(
        "click",
        "--repeat",
        str(abs(steps)),
        "--delay",
        "50",
        button,
    )

