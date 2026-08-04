"""Types shared by idle movement patterns and their controller."""

from __future__ import annotations

from typing import Protocol


class IdleMover(Protocol):
    def move(
        self, x: int, y: int, *, duration: float | None, steady: bool
    ) -> None: ...
