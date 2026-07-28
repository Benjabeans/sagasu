"""HumanCursor primary backend and explicit xdotool fallback."""

from __future__ import annotations

import math
import random
import subprocess
import sys
import time
from contextlib import redirect_stdout
from typing import Callable, Protocol

from sagasu.xcontrol.protocol import SagasuError
from sagasu.xcontrol.runtime.display import (
    Runner,
    get_pointer_position,
)


BUTTONS = {
    "left": ("left", 1),
    "1": ("left", 1),
    "middle": ("middle", 2),
    "2": ("middle", 2),
    "right": ("right", 3),
    "3": ("right", 3),
}


def normalize_button(button: str) -> tuple[str, int]:
    value = BUTTONS.get(button.casefold())
    if value is None:
        raise SagasuError(
            "invalid_arguments",
            "BUTTON must be left, middle, right, 1, 2, or 3",
            {"button": button},
            exit_status=2,
        )
    return value


class CursorBackend(Protocol):
    name: str

    def move(
        self, x: int, y: int, *, duration: float | None, steady: bool
    ) -> None: ...

    def click(
        self,
        x: int,
        y: int,
        *,
        button: str,
        count: int,
        hold: float,
    ) -> None: ...

    def drag(
        self,
        x1: int,
        y1: int,
        x2: int,
        y2: int,
        *,
        duration: float | None,
        steady: bool,
    ) -> None: ...

    def scroll(self, x: int, y: int, *, steps: int) -> None: ...


class HumanCursorBackend:
    name = "humancursor"

    def __init__(
        self,
        *,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        try:
            # python-xlib prints an expected "no xauthority details" warning to
            # stdout for Xvnc's intentionally unauthenticated local display.
            # Third-party chatter must never corrupt our JSON stdout protocol.
            with redirect_stdout(sys.stderr):
                import pyautogui
                from humancursor import SystemCursor
        except Exception as exc:
            raise SagasuError(
                "input_failed",
                "HumanCursor could not be loaded",
                {"backend": self.name, "reason": str(exc)},
            ) from exc
        # A full-display API must allow the valid coordinate (0, 0). PyAutoGUI's
        # desktop failsafe is unnecessary inside an isolated Xvnc session and
        # would turn that coordinate into a delayed failure.
        pyautogui.FAILSAFE = False
        self._pyautogui = pyautogui
        with redirect_stdout(sys.stderr):
            self._cursor = SystemCursor()
        self._sleep = sleep

    def _perform(self, operation: str, action: Callable[[], None]) -> None:
        try:
            with redirect_stdout(sys.stderr):
                action()
        except SagasuError:
            raise
        except Exception as exc:
            raise SagasuError(
                "input_failed",
                f"HumanCursor {operation} failed",
                {"backend": self.name, "reason": str(exc)},
            ) from exc

    def move(
        self, x: int, y: int, *, duration: float | None, steady: bool
    ) -> None:
        self._perform(
            "move",
            lambda: self._cursor.move_to(
                [x, y], duration=duration, steady=steady
            ),
        )

    def click(
        self,
        x: int,
        y: int,
        *,
        button: str,
        count: int,
        hold: float,
    ) -> None:
        button_name, _ = normalize_button(button)

        def action() -> None:
            if button_name == "left":
                self._cursor.click_on(
                    [x, y],
                    clicks=count,
                    click_duration=hold,
                )
                return
            self._cursor.move_to([x, y])
            for click_number in range(count):
                self._pyautogui.mouseDown(button=button_name)
                try:
                    self._sleep(hold)
                finally:
                    self._pyautogui.mouseUp(button=button_name)
                if click_number + 1 < count:
                    self._sleep(random.uniform(0.170, 0.280))

        self._perform("click", action)

    def drag(
        self,
        x1: int,
        y1: int,
        x2: int,
        y2: int,
        *,
        duration: float | None,
        steady: bool,
    ) -> None:
        def action() -> None:
            try:
                self._cursor.drag_and_drop(
                    [x1, y1],
                    [x2, y2],
                    duration=duration,
                    steady=steady,
                )
            except Exception:
                # HumanCursor does not use a finally block around mouseUp.
                # Release the primary button before surfacing its failure.
                try:
                    self._pyautogui.mouseUp(button="left")
                except Exception:
                    pass
                raise

        self._perform("drag", action)

    def scroll(self, x: int, y: int, *, steps: int) -> None:
        def action() -> None:
            self._cursor.move_to([x, y])
            # PyAutoGUI and xdotool agree: positive is wheel-up.
            self._pyautogui.scroll(steps)

        self._perform("scroll", action)


class XDoToolBackend:
    name = "xdotool"

    def __init__(
        self,
        *,
        runner: Runner = subprocess.run,
        sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._runner = runner
        self._sleep = sleep
        self._monotonic = monotonic

    def _call(self, *arguments: str) -> None:
        try:
            completed = self._runner(
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
                {"backend": self.name},
            ) from exc
        except OSError as exc:
            raise SagasuError(
                "input_failed",
                "xdotool could not be started",
                {"backend": self.name, "reason": str(exc)},
            ) from exc
        if completed.returncode:
            raise SagasuError(
                "input_failed",
                "xdotool input failed",
                {
                    "backend": self.name,
                    "reason": completed.stderr.decode(
                        "utf-8", errors="replace"
                    ).strip(),
                },
            )

    def move(
        self, x: int, y: int, *, duration: float | None, steady: bool
    ) -> None:
        del steady
        if not duration:
            self._call("mousemove", "--sync", str(x), str(y))
            return
        start = get_pointer_position(runner=self._runner)
        frames = max(2, min(120, math.ceil(duration * 60)))
        started = self._monotonic()
        for frame in range(1, frames + 1):
            fraction = frame / frames
            next_x = round(start.x + (x - start.x) * fraction)
            next_y = round(start.y + (y - start.y) * fraction)
            self._call("mousemove", "--sync", str(next_x), str(next_y))
            deadline = started + duration * fraction
            remaining = deadline - self._monotonic()
            if remaining > 0:
                self._sleep(remaining)

    def click(
        self,
        x: int,
        y: int,
        *,
        button: str,
        count: int,
        hold: float,
    ) -> None:
        _, button_number = normalize_button(button)
        self.move(x, y, duration=None, steady=False)
        for click_number in range(count):
            self._call("mousedown", str(button_number))
            try:
                self._sleep(hold)
            finally:
                self._call("mouseup", str(button_number))
            if click_number + 1 < count:
                self._sleep(0.2)

    def drag(
        self,
        x1: int,
        y1: int,
        x2: int,
        y2: int,
        *,
        duration: float | None,
        steady: bool,
    ) -> None:
        first_duration = None if duration is None else duration / 2
        second_duration = first_duration
        self.move(x1, y1, duration=first_duration, steady=steady)
        self._call("mousedown", "1")
        try:
            self.move(x2, y2, duration=second_duration, steady=steady)
        finally:
            self._call("mouseup", "1")

    def scroll(self, x: int, y: int, *, steps: int) -> None:
        self.move(x, y, duration=None, steady=False)
        button = "4" if steps > 0 else "5"
        self._call(
            "click",
            "--repeat",
            str(abs(steps)),
            "--delay",
            "50",
            button,
        )


def create_backend(name: str) -> CursorBackend:
    if name == "humancursor":
        return HumanCursorBackend()
    if name == "xdotool":
        return XDoToolBackend()
    raise SagasuError(
        "invalid_arguments",
        "BACKEND must be humancursor or xdotool",
        {"backend": name},
        exit_status=2,
    )
