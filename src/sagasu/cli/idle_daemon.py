"""Per-container daemon for continuous HumanCursor idle movement."""

from __future__ import annotations

import math
import os
import random
import signal
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from threading import Event
from typing import Callable, Mapping, TextIO

from sagasu.protocol import SagasuError
from sagasu.sessions import human_control
from sagasu.sessions.activity import (
    ACTIVITY_PATH,
    IDLE_GATE_PATH,
    idle_gate,
    read_activity,
    record_activity,
)
from sagasu.sessions.locking import LOCK_PATH, session_lock
from sagasu.xcontrol.cursor import HumanCursorBackend
from sagasu.xcontrol.display import (
    DisplaySize,
    PointerPosition,
    get_display_size,
    get_pointer_position,
)
from sagasu.xcontrol.idle import IdleMover, random_target


@dataclass(frozen=True)
class IdleConfig:
    after_seconds: float = 5.0
    radius_pixels: int = 300
    minimum_duration_seconds: float = 0.3
    maximum_duration_seconds: float = 2.0
    poll_seconds: float = 0.25
    error_backoff_seconds: float = 5.0

    @classmethod
    def from_environ(cls, environ: Mapping[str, str]) -> "IdleConfig":
        minimum_duration = _environment_float(
            environ,
            "SAGASU_IDLE_MIN_DURATION_SECONDS",
            0.3,
            minimum=0.01,
        )
        maximum_duration = _environment_float(
            environ,
            "SAGASU_IDLE_MAX_DURATION_SECONDS",
            2.0,
            minimum=0.01,
        )
        if maximum_duration < minimum_duration:
            raise SagasuError(
                "invalid_arguments",
                "SAGASU_IDLE_MAX_DURATION_SECONDS must not be less than "
                "SAGASU_IDLE_MIN_DURATION_SECONDS",
                exit_status=2,
            )
        return cls(
            after_seconds=_environment_float(
                environ, "SAGASU_IDLE_AFTER_SECONDS", 5.0, minimum=0.0
            ),
            radius_pixels=_environment_int(
                environ, "SAGASU_IDLE_RADIUS_PX", 300, minimum=1
            ),
            minimum_duration_seconds=minimum_duration,
            maximum_duration_seconds=maximum_duration,
        )


class IdleController:
    """Run one complete HumanCursor movement per ``step`` while idle."""

    def __init__(
        self,
        config: IdleConfig,
        mover: IdleMover,
        *,
        activity_path: Path | str = ACTIVITY_PATH,
        gate_path: Path | str = IDLE_GATE_PATH,
        lock_path: Path | str = LOCK_PATH,
        pause_path: Path | str = human_control.PAUSE_PATH,
        clock_ns: Callable[[], int] = time.monotonic_ns,
        display_size: Callable[[], DisplaySize] = get_display_size,
        pointer_position: Callable[[], PointerPosition] = get_pointer_position,
        random_value: Callable[[], float] = random.random,
        random_duration: Callable[[float, float], float] = random.uniform,
    ) -> None:
        self.config = config
        self.mover = mover
        self.activity_path = Path(activity_path)
        self.gate_path = Path(gate_path)
        self.lock_path = Path(lock_path)
        self.pause_path = Path(pause_path)
        self._clock_ns = clock_ns
        self._display_size = display_size
        self._pointer_position = pointer_position
        self._random_value = random_value
        self._random_duration = random_duration
        self._seen_activity: int | None = None
        self._anchor: PointerPosition | None = None
        self._expected_pointer: PointerPosition | None = None
        self._was_paused = False

    def initialize(self) -> None:
        self._seen_activity = record_activity(
            self.activity_path,
            now_ns=self._clock_ns,
        )

    def step(self) -> float:
        """Advance the state machine and return the next polling delay."""

        if self._seen_activity is None:
            self.initialize()
        assert self._seen_activity is not None

        now = self._clock_ns()
        activity = read_activity(self.activity_path)
        if self.pause_path.exists():
            self._reset_idle()
            self._was_paused = True
            return self.config.poll_seconds
        if self._was_paused:
            activity = record_activity(
                self.activity_path,
                now_ns=self._clock_ns,
            )
            self._seen_activity = activity
            self._reset_idle()
            self._was_paused = False
            return self.config.poll_seconds

        if activity != self._seen_activity:
            self._seen_activity = activity
            self._reset_idle()

        idle_deadline = activity + self._seconds_ns(
            self.config.after_seconds
        )
        if now < idle_deadline:
            return self._bounded_delay(idle_deadline - now)
        return self._advance_movement(activity)

    def run(
        self,
        stop: Event,
        *,
        log: Callable[[str], None],
    ) -> None:
        self.initialize()
        while not stop.is_set():
            try:
                delay = self.step()
            except Exception as exc:  # daemon faults must not end the browser
                self._reset_idle()
                log(f"idle controller error: {exc}")
                delay = self.config.error_backoff_seconds
            stop.wait(max(0.001, delay))

    def _advance_movement(self, activity: int) -> float:
        with idle_gate(self.gate_path) as acquired:
            if not acquired:
                self._reset_idle()
                return self.config.poll_seconds
            try:
                with session_lock(exclusive=True, path=self.lock_path):
                    return self._advance_movement_locked(activity)
            except SagasuError as exc:
                if exc.code != "session_busy":
                    raise
                self._reset_idle()
                return self.config.poll_seconds

    def _advance_movement_locked(self, activity: int) -> float:
        if self.pause_path.exists() or read_activity(self.activity_path) != activity:
            self._reset_idle()
            return self.config.poll_seconds

        pointer = self._pointer_position()
        if self._expected_pointer is not None and pointer != self._expected_pointer:
            external_activity = record_activity(
                self.activity_path,
                now_ns=self._clock_ns,
            )
            self._seen_activity = external_activity
            self._reset_idle()
            return self.config.poll_seconds

        if self._anchor is None:
            self._anchor = pointer
        target = random_target(
            self._anchor,
            pointer,
            self._display_size(),
            radius=self.config.radius_pixels,
            random_value=self._random_value,
        )
        duration = self._random_duration(
            self.config.minimum_duration_seconds,
            self.config.maximum_duration_seconds,
        )
        self.mover.move(
            target.x,
            target.y,
            duration=duration,
            steady=False,
        )
        self._expected_pointer = target
        # The movement itself supplies pacing. Re-enter immediately so motion
        # remains continuous unless a command has announced new activity.
        return 0.001

    def _reset_idle(self) -> None:
        self._anchor = None
        self._expected_pointer = None

    def _bounded_delay(self, remaining_ns: int) -> float:
        remaining = max(0.0, remaining_ns / 1_000_000_000)
        return max(0.001, min(self.config.poll_seconds, remaining))

    @staticmethod
    def _seconds_ns(seconds: float) -> int:
        return round(seconds * 1_000_000_000)


def main() -> int:
    try:
        config = IdleConfig.from_environ(os.environ)
        mover = HumanCursorBackend()
    except SagasuError as exc:
        _log(f"startup error: {exc.message}")
        return exc.exit_status

    stop = Event()

    def request_stop(signum: int, frame: object) -> None:
        del signum, frame
        stop.set()

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)
    _log(
        "started "
        f"(after={config.after_seconds:g}s, backend=humancursor, "
        f"radius={config.radius_pixels}px, "
        f"duration={config.minimum_duration_seconds:g}-"
        f"{config.maximum_duration_seconds:g}s, continuous=true)"
    )
    IdleController(config, mover).run(stop, log=_log)
    _log("stopped")
    return 0


def _environment_float(
    environ: Mapping[str, str],
    name: str,
    default: float,
    *,
    minimum: float,
) -> float:
    raw = environ.get(name, str(default))
    try:
        value = float(raw)
    except ValueError as exc:
        raise _environment_error(name, raw) from exc
    if not math.isfinite(value) or value < minimum:
        raise _environment_error(name, raw)
    return value


def _environment_int(
    environ: Mapping[str, str],
    name: str,
    default: int,
    *,
    minimum: int,
) -> int:
    raw = environ.get(name, str(default))
    try:
        value = int(raw)
    except ValueError as exc:
        raise _environment_error(name, raw) from exc
    if value < minimum:
        raise _environment_error(name, raw)
    return value


def _environment_error(name: str, value: str) -> SagasuError:
    return SagasuError(
        "invalid_arguments",
        f"{name} has an invalid value",
        {"name": name, "value": value},
        exit_status=2,
    )


def _log(message: str, stream: TextIO = sys.stderr) -> None:
    stream.write(f"[sagasu/idle] {message}\n")
    stream.flush()


if __name__ == "__main__":
    raise SystemExit(main())
