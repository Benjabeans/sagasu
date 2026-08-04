"""Per-session activity epochs and agent-priority idle arbitration."""

from __future__ import annotations

import errno
import fcntl
import os
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Callable, Iterator

from sagasu.protocol import SagasuError


ACTIVITY_PATH = Path("/run/sagasu/xcontrol.activity")
IDLE_GATE_PATH = Path("/run/sagasu/xcontrol.idle.lock")
# HumanCursor idle trajectories last at most two configured seconds. Commands
# announce activity before waiting, then allow the current curve a small
# cleanup margin before reporting a genuinely stuck idle controller.
AGENT_IDLE_YIELD_TIMEOUT_SECONDS = 3.0
AGENT_IDLE_YIELD_POLL_SECONDS = 0.005


def paths_for_lock(lock_path: Path | str) -> tuple[Path, Path]:
    """Resolve idle runtime files beside a custom or default X lock."""

    runtime_directory = Path(lock_path).parent
    return (
        runtime_directory / ACTIVITY_PATH.name,
        runtime_directory / IDLE_GATE_PATH.name,
    )


def record_activity(
    path: Path | str = ACTIVITY_PATH,
    *,
    now_ns: Callable[[], int] = time.monotonic_ns,
) -> int:
    """Atomically advance and return the per-container activity epoch."""

    activity_path = Path(path)
    descriptor = _open_state_file(activity_path, "activity epoch")
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        previous = _read_epoch(descriptor, activity_path)
        current = max(int(now_ns()), previous + 1)
        payload = f"{current}\n".encode("ascii")
        os.ftruncate(descriptor, 0)
        os.lseek(descriptor, 0, os.SEEK_SET)
        _write_all(descriptor, payload)
        return current
    except SagasuError:
        raise
    except OSError as exc:
        raise SagasuError(
            "input_failed",
            "The session activity epoch could not be updated",
            {"path": str(activity_path), "reason": str(exc)},
        ) from exc
    finally:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)


def read_activity(path: Path | str = ACTIVITY_PATH) -> int:
    """Read the latest activity epoch, returning zero before initialization."""

    activity_path = Path(path)
    descriptor = _open_state_file(activity_path, "activity epoch")
    try:
        fcntl.flock(descriptor, fcntl.LOCK_SH)
        return _read_epoch(descriptor, activity_path)
    except SagasuError:
        raise
    except OSError as exc:
        raise SagasuError(
            "input_failed",
            "The session activity epoch could not be read",
            {"path": str(activity_path), "reason": str(exc)},
        ) from exc
    finally:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)


@contextmanager
def agent_activity(
    *,
    activity_path: Path | str = ACTIVITY_PATH,
    gate_path: Path | str = IDLE_GATE_PATH,
    now_ns: Callable[[], int] = time.monotonic_ns,
    monotonic: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
    yield_timeout: float = AGENT_IDLE_YIELD_TIMEOUT_SECONDS,
) -> Iterator[None]:
    """Announce one command and prevent idle frames until it completes."""

    record_activity(activity_path, now_ns=now_ns)
    gate = Path(gate_path)
    descriptor = _open_state_file(gate, "idle gate")
    acquired = False
    deadline = monotonic() + yield_timeout
    try:
        while True:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_SH | fcntl.LOCK_NB)
                acquired = True
                break
            except OSError as exc:
                if exc.errno not in (errno.EACCES, errno.EAGAIN):
                    raise SagasuError(
                        "input_failed",
                        "The idle gate could not be acquired",
                        {"path": str(gate), "reason": str(exc)},
                    ) from exc
                remaining = deadline - monotonic()
                if remaining <= 0:
                    raise SagasuError(
                        "session_busy",
                        "Idle cursor movement did not yield to the command",
                    ) from exc
                sleep(min(AGENT_IDLE_YIELD_POLL_SECONDS, remaining))
        try:
            yield
        finally:
            record_activity(activity_path, now_ns=now_ns)
    finally:
        if acquired:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


@contextmanager
def idle_gate(
    path: Path | str = IDLE_GATE_PATH,
) -> Iterator[bool]:
    """Try to exclude commands for one complete idle movement."""

    gate_path = Path(path)
    descriptor = _open_state_file(gate_path, "idle gate")
    acquired = False
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            acquired = True
        except OSError as exc:
            if exc.errno not in (errno.EACCES, errno.EAGAIN):
                raise SagasuError(
                    "input_failed",
                    "The idle gate could not be acquired",
                    {"path": str(gate_path), "reason": str(exc)},
                ) from exc
        yield acquired
    finally:
        if acquired:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def _open_state_file(path: Path, description: str) -> int:
    try:
        return os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    except OSError as exc:
        raise SagasuError(
            "input_failed",
            f"The session {description} could not be opened",
            {"path": str(path), "reason": str(exc)},
        ) from exc


def _read_epoch(descriptor: int, path: Path) -> int:
    os.lseek(descriptor, 0, os.SEEK_SET)
    payload = os.read(descriptor, 64)
    if not payload:
        return 0
    try:
        value = int(payload.decode("ascii").strip())
    except (UnicodeDecodeError, ValueError) as exc:
        raise SagasuError(
            "input_failed",
            "The session activity epoch is invalid",
            {"path": str(path)},
        ) from exc
    if value < 0:
        raise SagasuError(
            "input_failed",
            "The session activity epoch is invalid",
            {"path": str(path)},
        )
    return value


def _write_all(descriptor: int, payload: bytes) -> None:
    offset = 0
    while offset < len(payload):
        written = os.write(descriptor, payload[offset:])
        if written <= 0:  # pragma: no cover - regular files make progress
            raise OSError("activity epoch write made no progress")
        offset += written
