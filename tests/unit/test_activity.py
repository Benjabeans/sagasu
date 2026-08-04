from __future__ import annotations

import pytest

from sagasu.protocol import SagasuError
from sagasu.sessions.activity import (
    agent_activity,
    idle_gate,
    paths_for_lock,
    read_activity,
    record_activity,
)


def test_activity_epoch_is_atomic_and_strictly_increases(tmp_path):
    path = tmp_path / "activity"

    first = record_activity(path, now_ns=lambda: 100)
    second = record_activity(path, now_ns=lambda: 50)

    assert first == 100
    assert second == 101
    assert read_activity(path) == second


def test_agent_activity_excludes_idle_frames_and_records_completion(tmp_path):
    activity_path = tmp_path / "activity"
    gate_path = tmp_path / "idle.lock"

    with agent_activity(
        activity_path=activity_path,
        gate_path=gate_path,
        now_ns=lambda: 200,
        yield_timeout=0,
    ):
        started = read_activity(activity_path)
        with idle_gate(gate_path) as acquired:
            assert acquired is False

    assert read_activity(activity_path) > started
    with idle_gate(gate_path) as acquired:
        assert acquired is True


def test_agent_command_times_out_if_an_idle_frame_does_not_yield(tmp_path):
    activity_path = tmp_path / "activity"
    gate_path = tmp_path / "idle.lock"

    with idle_gate(gate_path) as acquired:
        assert acquired is True
        with pytest.raises(SagasuError) as error:
            with agent_activity(
                activity_path=activity_path,
                gate_path=gate_path,
                now_ns=lambda: 300,
                yield_timeout=0,
            ):
                pass

    assert error.value.code == "session_busy"
    assert read_activity(activity_path) == 300


def test_activity_paths_follow_each_container_lock_directory(tmp_path):
    first = paths_for_lock(tmp_path / "one" / "xcontrol.lock")
    second = paths_for_lock(tmp_path / "two" / "xcontrol.lock")

    assert first[0].parent.name == "one"
    assert first[1].parent.name == "one"
    assert second[0].parent.name == "two"
    assert first != second
