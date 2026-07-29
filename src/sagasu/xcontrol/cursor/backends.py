"""HumanCursor primary backend and explicit xdotool fallback."""

from __future__ import annotations

import subprocess
import sys
import time
from contextlib import redirect_stdout
from typing import Callable

from sagasu.protocol import SagasuError
from sagasu.xcontrol.cursor.click import human_click, xdotool_click
from sagasu.xcontrol.cursor.drag import human_drag, xdotool_drag
from sagasu.xcontrol.cursor.move import human_move, xdotool_move
from sagasu.xcontrol.cursor.scroll import human_scroll, xdotool_scroll
from sagasu.xcontrol.cursor.types import (
    CursorBackend,
    HumanCursorDriver,
    PyAutoGUI,
)
from sagasu.xcontrol.display import Runner


class HumanCursorBackend:
    name = "humancursor"

    def __init__(
        self,
        *,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        try:
            # python-xlib prints an expected Xvnc xauthority warning to stdout.
            # Third-party chatter must never corrupt the JSON stdout protocol.
            with redirect_stdout(sys.stderr):
                import pyautogui
                from humancursor import SystemCursor
        except Exception as exc:
            raise SagasuError(
                "input_failed",
                "HumanCursor could not be loaded",
                {"backend": self.name, "reason": str(exc)},
            ) from exc
        # Full-display coordinates include (0, 0); disable PyAutoGUI's desktop
        # failsafe inside this isolated Xvnc session.
        pyautogui.FAILSAFE = False
        self._pyautogui: PyAutoGUI = pyautogui
        with redirect_stdout(sys.stderr):
            self._cursor: HumanCursorDriver = SystemCursor()
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
            lambda: human_move(
                self._cursor,
                x,
                y,
                duration=duration,
                steady=steady,
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
        self._perform(
            "click",
            lambda: human_click(
                self._cursor,
                self._pyautogui,
                x,
                y,
                button=button,
                count=count,
                hold=hold,
                sleep=self._sleep,
            ),
        )

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
        self._perform(
            "drag",
            lambda: human_drag(
                self._cursor,
                self._pyautogui,
                x1,
                y1,
                x2,
                y2,
                duration=duration,
                steady=steady,
            ),
        )

    def scroll(self, x: int, y: int, *, steps: int) -> None:
        self._perform(
            "scroll",
            lambda: human_scroll(
                self._cursor,
                self._pyautogui,
                x,
                y,
                steps=steps,
            ),
        )


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
        xdotool_move(
            self._call,
            x,
            y,
            duration=duration,
            runner=self._runner,
            sleep=self._sleep,
            monotonic=self._monotonic,
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
        xdotool_click(
            self.move,
            self._call,
            x,
            y,
            button=button,
            count=count,
            hold=hold,
            sleep=self._sleep,
        )

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
        xdotool_drag(
            self.move,
            self._call,
            x1,
            y1,
            x2,
            y2,
            duration=duration,
            steady=steady,
        )

    def scroll(self, x: int, y: int, *, steps: int) -> None:
        xdotool_scroll(
            self.move,
            self._call,
            x,
            y,
            steps=steps,
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

