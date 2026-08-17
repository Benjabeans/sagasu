"""Random bounded destinations for idle cursor movement."""

from __future__ import annotations

import math
import random
from collections.abc import Callable

from sagasu.xcontrol.display import DisplaySize, PointerPosition


def random_target(
    anchor: PointerPosition,
    current: PointerPosition,
    display: DisplaySize,
    *,
    radius: int,
    random_value: Callable[[], float] = random.random,
) -> PointerPosition:
    """Choose a visible point within ``radius`` of the fixed idle anchor."""

    for _ in range(12):
        # sqrt makes samples uniform by area rather than clustering at center.
        distance = radius * math.sqrt(_unit(random_value()))
        angle = math.tau * _unit(random_value())
        candidate = PointerPosition(
            x=_clamp(
                round(anchor.x + distance * math.cos(angle)),
                0,
                display.width - 1,
            ),
            y=_clamp(
                round(anchor.y + distance * math.sin(angle)),
                0,
                display.height - 1,
            ),
        )
        if candidate != current and _distance(anchor, candidate) <= radius:
            return candidate

    # Deterministic fallback for a pathological random source. Clamping moves
    # candidates toward an on-screen anchor, so it cannot increase distance.
    for candidate in (
        PointerPosition(_clamp(anchor.x + radius, 0, display.width - 1), anchor.y),
        PointerPosition(_clamp(anchor.x - radius, 0, display.width - 1), anchor.y),
        PointerPosition(anchor.x, _clamp(anchor.y + radius, 0, display.height - 1)),
        PointerPosition(anchor.x, _clamp(anchor.y - radius, 0, display.height - 1)),
    ):
        if candidate != current:
            return candidate
    return current


def _unit(value: float) -> float:
    return max(0.0, min(value, 1.0))


def _distance(first: PointerPosition, second: PointerPosition) -> float:
    return math.hypot(second.x - first.x, second.y - first.y)


def _clamp(value: int, lower: int, upper: int) -> int:
    return max(lower, min(value, upper))
