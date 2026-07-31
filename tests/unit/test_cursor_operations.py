from __future__ import annotations

import pytest

from sagasu.xcontrol.cursor.click import human_click, xdotool_click
from sagasu.xcontrol.cursor.drag import human_drag, xdotool_drag
from sagasu.xcontrol.cursor.scroll import human_scroll, xdotool_scroll


class FakeHumanCursor:
    def __init__(self, *, drag_error: Exception | None = None):
        self.calls = []
        self.drag_error = drag_error

    def move_to(self, point, **options):
        self.calls.append(("move_to", point, options))

    def click_on(self, point, **options):
        self.calls.append(("click_on", point, options))

    def drag_and_drop(self, start, end, **options):
        self.calls.append(("drag_and_drop", start, end, options))
        if self.drag_error is not None:
            raise self.drag_error


class FakePyAutoGUI:
    FAILSAFE = True

    def __init__(self):
        self.calls = []

    def mouseDown(self, *, button):
        self.calls.append(("down", button))

    def mouseUp(self, *, button):
        self.calls.append(("up", button))

    def scroll(self, steps):
        self.calls.append(("scroll", steps))


def test_human_click_uses_native_left_click_and_explicit_other_buttons():
    cursor = FakeHumanCursor()
    gui = FakePyAutoGUI()
    sleeps = []

    human_click(
        cursor,
        gui,
        10,
        20,
        button="left",
        count=2,
        hold=0.03,
        sleep=sleeps.append,
    )
    assert cursor.calls == [
        (
            "click_on",
            [10, 20],
            {"clicks": 2, "click_duration": 0.03},
        )
    ]

    cursor.calls.clear()
    human_click(
        cursor,
        gui,
        30,
        40,
        button="right",
        count=2,
        hold=0.01,
        sleep=sleeps.append,
        random_interval=lambda start, end: 0.2,
    )
    assert cursor.calls == [("move_to", [30, 40], {})]
    assert gui.calls == [
        ("down", "right"),
        ("up", "right"),
        ("down", "right"),
        ("up", "right"),
    ]
    assert sleeps == [0.01, 0.2, 0.01]


def test_drag_implementations_always_release_after_failure():
    cursor = FakeHumanCursor(drag_error=RuntimeError("drag failed"))
    gui = FakePyAutoGUI()
    with pytest.raises(RuntimeError, match="drag failed"):
        human_drag(
            cursor,
            gui,
            1,
            2,
            3,
            4,
            duration=0.2,
            steady=True,
        )
    assert gui.calls == [("up", "left")]

    calls = []

    def move(x, y, *, duration, steady):
        calls.append(("move", x, y, duration, steady))
        if (x, y) == (3, 4):
            raise RuntimeError("move failed")

    def call(*arguments):
        calls.append(arguments)

    with pytest.raises(RuntimeError, match="move failed"):
        xdotool_drag(
            move,
            call,
            1,
            2,
            3,
            4,
            duration=0.4,
            steady=False,
        )
    assert ("mousedown", "1") in calls
    assert calls[-1] == ("mouseup", "1")


def test_scroll_direction_and_click_sequence_are_operation_local():
    calls = []

    def move(x, y, *, duration, steady):
        calls.append(("move", x, y, duration, steady))

    def call(*arguments):
        calls.append(arguments)

    xdotool_scroll(move, call, 5, 6, steps=3)
    assert calls[-1] == ("click", "--repeat", "3", "--delay", "50", "4")
    xdotool_scroll(move, call, 5, 6, steps=-2)
    assert calls[-1] == ("click", "--repeat", "2", "--delay", "50", "5")

    sleeps = []
    xdotool_click(
        move,
        call,
        7,
        8,
        button="middle",
        count=2,
        hold=0.05,
        sleep=sleeps.append,
    )
    assert ("mousedown", "2") in calls
    assert ("mouseup", "2") in calls
    assert sleeps == [0.05, 0.2, 0.05]


def test_human_scroll_moves_before_wheel_input():
    cursor = FakeHumanCursor()
    gui = FakePyAutoGUI()
    human_scroll(cursor, gui, 9, 10, steps=-4)
    assert cursor.calls == [("move_to", [9, 10], {})]
    assert gui.calls == [("scroll", -4)]

