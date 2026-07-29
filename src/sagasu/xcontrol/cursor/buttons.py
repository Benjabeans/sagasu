"""Mouse-button parsing shared by cursor actions."""

from __future__ import annotations

from sagasu.protocol import SagasuError


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

