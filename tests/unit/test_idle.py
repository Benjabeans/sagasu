from __future__ import annotations

import math
import random

import pytest

from sagasu.cli.idle_daemon import IdleConfig, IdleController
from sagasu.protocol import SagasuError
from sagasu.sessions.activity import read_activity, record_activity
from sagasu.xcontrol.display import DisplaySize, PointerPosition
from sagasu.xcontrol.idle.patterns.random_target import random_target


class FakeClock:
    def __init__(self) -> None:
        self.value = 0

    def __call__(self) -> int:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += round(seconds * 1_000_000_000)


class FakeMover:
    def __init__(self, pointer: list[PointerPosition]) -> None:
        self.pointer = pointer
        self.calls: list[tuple[int, int, float | None, bool]] = []

    def move(self, x, y, *, duration, steady):
        self.calls.append((x, y, duration, steady))
        self.pointer[0] = PointerPosition(x, y)


def controller(
    tmp_path,
    clock,
    pointer,
    *,
    after=1.0,
    radius=30,
    random_duration=None,
):
    mover = FakeMover(pointer)
    generator = random.Random(17)

    def fixed_duration(minimum, maximum):
        assert minimum == 0.3
        assert maximum == 2.0
        return 0.75

    instance = IdleController(
        IdleConfig(
            after_seconds=after,
            radius_pixels=radius,
            minimum_duration_seconds=0.3,
            maximum_duration_seconds=2.0,
            poll_seconds=0.1,
        ),
        mover,
        activity_path=tmp_path / "activity",
        gate_path=tmp_path / "idle.lock",
        lock_path=tmp_path / "xcontrol.lock",
        pause_path=tmp_path / "paused",
        clock_ns=clock,
        display_size=lambda: DisplaySize(100, 80),
        pointer_position=lambda: pointer[0],
        random_value=generator.random,
        random_duration=random_duration or fixed_duration,
    )
    return instance, mover


def test_random_targets_stay_on_screen_and_within_the_fixed_radius():
    generator = random.Random(42)
    anchor = PointerPosition(500, 350)
    current = anchor
    targets = []

    for _ in range(200):
        current = random_target(
            anchor,
            current,
            DisplaySize(1366, 768),
            radius=300,
            random_value=generator.random,
        )
        targets.append(current)

    assert len(set(targets)) > 150
    assert all(0 <= target.x < 1366 for target in targets)
    assert all(0 <= target.y < 768 for target in targets)
    assert all(
        math.hypot(target.x - anchor.x, target.y - anchor.y) <= 300
        for target in targets
    )


def test_random_target_clamps_at_edges_and_avoids_a_noop():
    anchor = PointerPosition(0, 0)
    target = random_target(
        anchor,
        anchor,
        DisplaySize(100, 80),
        radius=300,
        random_value=lambda: 0.0,
    )

    assert target != anchor
    assert 0 <= target.x < 100
    assert 0 <= target.y < 80
    assert math.hypot(target.x, target.y) <= 300


def test_controller_waits_for_idle_then_moves_continuously(tmp_path):
    clock = FakeClock()
    pointer = [PointerPosition(40, 30)]
    idle, mover = controller(tmp_path, clock, pointer)
    idle.initialize()

    clock.advance(0.9)
    idle.step()
    assert mover.calls == []

    clock.advance(0.2)
    idle.step()
    idle.step()
    assert len(mover.calls) == 2
    assert all(call[2:] == (0.75, False) for call in mover.calls)

    record_activity(tmp_path / "activity", now_ns=clock)
    idle.step()
    assert len(mover.calls) == 2


def test_continuous_destinations_keep_the_original_idle_anchor(tmp_path):
    clock = FakeClock()
    anchor = PointerPosition(40, 30)
    pointer = [anchor]
    duration_generator = random.Random(91)
    idle, mover = controller(
        tmp_path,
        clock,
        pointer,
        radius=20,
        random_duration=duration_generator.uniform,
    )
    idle.initialize()
    clock.advance(1.1)

    for _ in range(30):
        idle.step()

    assert len(mover.calls) == 30
    assert all(
        math.hypot(x - anchor.x, y - anchor.y) <= 20
        for x, y, _, _ in mover.calls
    )
    durations = [duration for _, _, duration, _ in mover.calls]
    assert all(duration is not None and 0.3 <= duration <= 2 for duration in durations)
    assert len(set(durations)) > 20


def test_human_pause_stops_idle_and_resume_starts_a_fresh_cooldown(tmp_path):
    clock = FakeClock()
    pointer = [PointerPosition(40, 30)]
    idle, mover = controller(tmp_path, clock, pointer)
    idle.initialize()
    paused = tmp_path / "paused"
    paused.write_text("paused\n")

    clock.advance(2.0)
    idle.step()
    assert mover.calls == []

    paused.unlink()
    idle.step()
    clock.advance(0.9)
    idle.step()
    assert mover.calls == []

    clock.advance(0.2)
    idle.step()
    assert len(mover.calls) == 1


def test_unexpected_pointer_movement_stops_idle_and_resets_activity(tmp_path):
    clock = FakeClock()
    pointer = [PointerPosition(40, 30)]
    idle, mover = controller(tmp_path, clock, pointer)
    idle.initialize()
    clock.advance(1.1)
    idle.step()
    assert len(mover.calls) == 1
    prior_activity = read_activity(tmp_path / "activity")

    pointer[0] = PointerPosition(3, 4)
    idle.step()

    assert len(mover.calls) == 1
    assert read_activity(tmp_path / "activity") > prior_activity
    clock.advance(0.9)
    idle.step()
    assert len(mover.calls) == 1


def test_idle_configuration_is_validated():
    defaults = IdleConfig.from_environ({})
    assert defaults.after_seconds == 5
    assert defaults.radius_pixels == 300
    assert defaults.minimum_duration_seconds == 0.3
    assert defaults.maximum_duration_seconds == 2

    configured = IdleConfig.from_environ(
        {
            "SAGASU_IDLE_AFTER_SECONDS": "7",
            "SAGASU_IDLE_RADIUS_PX": "250",
            "SAGASU_IDLE_MIN_DURATION_SECONDS": "0.4",
            "SAGASU_IDLE_MAX_DURATION_SECONDS": "1.5",
        }
    )
    assert configured.after_seconds == 7
    assert configured.radius_pixels == 250
    assert configured.minimum_duration_seconds == 0.4
    assert configured.maximum_duration_seconds == 1.5

    invalid_environments = (
        {"SAGASU_IDLE_RADIUS_PX": "0"},
        {"SAGASU_IDLE_AFTER_SECONDS": "not-a-number"},
        {"SAGASU_IDLE_AFTER_SECONDS": "nan"},
        {
            "SAGASU_IDLE_MIN_DURATION_SECONDS": "1",
            "SAGASU_IDLE_MAX_DURATION_SECONDS": "0.5",
        },
    )
    for environment in invalid_environments:
        with pytest.raises(SagasuError):
            IdleConfig.from_environ(environment)
