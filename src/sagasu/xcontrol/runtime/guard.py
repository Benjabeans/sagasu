"""Human-control pause marker management."""

from __future__ import annotations

import os
from pathlib import Path

from sagasu.xcontrol.protocol import SagasuError


PAUSE_PATH = Path("/run/sagasu/xcontrol.paused")


def require_agent_control(path: Path | str = PAUSE_PATH) -> None:
    pause_path = Path(path)
    if pause_path.exists():
        raise SagasuError(
            "human_control",
            "Agent input is paused while a human controls this session",
        )


def pause(path: Path | str = PAUSE_PATH) -> None:
    pause_path = Path(path)
    try:
        descriptor = os.open(
            pause_path,
            os.O_WRONLY | os.O_CREAT | os.O_TRUNC,
            0o600,
        )
        try:
            os.write(descriptor, b"paused\n")
        finally:
            os.close(descriptor)
    except OSError as exc:
        raise SagasuError(
            "input_failed",
            "The human-control pause marker could not be created",
            {"path": str(pause_path), "reason": str(exc)},
        ) from exc


def resume(path: Path | str = PAUSE_PATH) -> None:
    pause_path = Path(path)
    try:
        pause_path.unlink(missing_ok=True)
    except OSError as exc:
        raise SagasuError(
            "input_failed",
            "The human-control pause marker could not be removed",
            {"path": str(pause_path), "reason": str(exc)},
        ) from exc

