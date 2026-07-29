"""Move-and-click implementations."""

from __future__ import annotations

import random
from typing import Callable

from sagasu.xcontrol.cursor.buttons import normalize_button
from sagasu.xcontrol.cursor.types import (
    HumanCursorDriver,
    MoveCursor,
    PyAutoGUI,
    XDoToolCaller,
)


def human_click(
    cursor: HumanCursorDriver,
    pyautogui: PyAutoGUI,
    x: int,
    y: int,
    *,
    button: str,
    count: int,
    hold: float,
    sleep: Callable[[float], None],
    random_interval: Callable[[float, float], float] = random.uniform,
) -> None:
    button_name, _ = normalize_button(button)
    if button_name == "left":
        cursor.click_on(
            [x, y],
            clicks=count,
            click_duration=hold,
        )
        return
    cursor.move_to([x, y])
    for click_number in range(count):
        pyautogui.mouseDown(button=button_name)
        try:
            sleep(hold)
        finally:
            pyautogui.mouseUp(button=button_name)
        if click_number + 1 < count:
            sleep(random_interval(0.170, 0.280))


def xdotool_click(
    move: MoveCursor,
    call: XDoToolCaller,
    x: int,
    y: int,
    *,
    button: str,
    count: int,
    hold: float,
    sleep: Callable[[float], None],
) -> None:
    _, button_number = normalize_button(button)
    move(x, y, duration=None, steady=False)
    for click_number in range(count):
        call("mousedown", str(button_number))
        try:
            sleep(hold)
        finally:
            call("mouseup", str(button_number))
        if click_number + 1 < count:
            sleep(0.2)

