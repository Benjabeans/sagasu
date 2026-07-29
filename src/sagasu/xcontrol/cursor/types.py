"""Protocols shared by cursor backends and operation modules."""

from __future__ import annotations

from typing import Protocol


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


class HumanCursorDriver(Protocol):
    def move_to(
        self,
        point: list[int],
        *,
        duration: float | None = None,
        steady: bool = False,
    ) -> object: ...

    def click_on(
        self,
        point: list[int],
        *,
        clicks: int,
        click_duration: float,
    ) -> object: ...

    def drag_and_drop(
        self,
        start: list[int],
        end: list[int],
        *,
        duration: float | None,
        steady: bool,
    ) -> object: ...


class PyAutoGUI(Protocol):
    FAILSAFE: bool

    def mouseDown(self, *, button: str) -> object: ...

    def mouseUp(self, *, button: str) -> object: ...

    def scroll(self, steps: int) -> object: ...


class XDoToolCaller(Protocol):
    def __call__(self, *arguments: str) -> None: ...


class MoveCursor(Protocol):
    def __call__(
        self,
        x: int,
        y: int,
        *,
        duration: float | None,
        steady: bool,
    ) -> None: ...

