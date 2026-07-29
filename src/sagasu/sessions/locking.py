"""Per-display process lock for X observation and mutation."""

from __future__ import annotations

import errno
import fcntl
import os
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from sagasu.protocol import SagasuError


LOCK_PATH = Path("/run/sagasu/xcontrol.lock")


@contextmanager
def session_lock(
    *,
    exclusive: bool,
    path: Path | str = LOCK_PATH,
) -> Iterator[None]:
    """Take a nonblocking shared or exclusive session lock."""

    lock_path = Path(path)
    try:
        descriptor = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    except OSError as exc:
        raise SagasuError(
            "input_failed",
            "The session control lock could not be opened",
            {"path": str(lock_path), "reason": str(exc)},
        ) from exc

    operation = (fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH) | fcntl.LOCK_NB
    try:
        try:
            fcntl.flock(descriptor, operation)
        except OSError as exc:
            if exc.errno in (errno.EACCES, errno.EAGAIN):
                raise SagasuError(
                    "session_busy",
                    "Another actor is controlling this session",
                ) from exc
            raise SagasuError(
                "input_failed",
                "The session control lock could not be acquired",
                {"path": str(lock_path), "reason": str(exc)},
            ) from exc
        yield
    finally:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)
