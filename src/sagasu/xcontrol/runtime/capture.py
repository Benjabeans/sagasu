"""Stream a full-display PNG from scrot."""

from __future__ import annotations

import subprocess
from typing import BinaryIO, Callable

from sagasu.xcontrol.protocol import SagasuError


def stream_png(
    destination: BinaryIO,
    *,
    include_pointer: bool = True,
    runner: Callable[..., subprocess.CompletedProcess[bytes]] = subprocess.run,
) -> None:
    command = ["scrot", "--silent", "--format", "png"]
    if include_pointer:
        command.append("--pointer")
    command.extend(["--file", "-"])
    try:
        completed = runner(
            command,
            stdin=subprocess.DEVNULL,
            stdout=destination,
            stderr=subprocess.PIPE,
            check=False,
        )
    except FileNotFoundError as exc:
        raise SagasuError(
            "capture_failed",
            "scrot is not installed in the session container",
        ) from exc
    except OSError as exc:
        raise SagasuError(
            "capture_failed",
            "scrot could not be started",
            {"reason": str(exc)},
        ) from exc
    if completed.returncode:
        raise SagasuError(
            "capture_failed",
            "scrot could not capture the X display",
            {
                "reason": completed.stderr.decode(
                    "utf-8", errors="replace"
                ).strip()
            },
        )
    destination.flush()
