from __future__ import annotations

import json

import pytest

from sagasu.cdp.insert_text import TextInsertionResult
from sagasu.cli.action_sequence import (
    ActionSequenceConfig,
    CursorClick,
    PageNavigate,
    encode_action_sequence,
    parse_action_sequence,
    run_action_sequence,
    validate_sequence_coordinates,
)
from sagasu.protocol import SagasuError
from sagasu.xcontrol.display import DisplaySize, PointerPosition


class FakeBackend:
    name = "fake"

    def __init__(self, events):
        self.events = events

    def move(self, x, y, *, duration, steady):
        self.events.append(("move", x, y, duration, steady))

    def click(self, x, y, *, button, count, hold):
        self.events.append(("click", x, y, button, count, hold))

    def drag(self, x1, y1, x2, y2, *, duration, steady):
        self.events.append(
            ("drag", x1, y1, x2, y2, duration, steady)
        )

    def scroll(self, x, y, *, steps):
        self.events.append(("scroll", x, y, steps))


def document(*actions):
    return json.dumps(actions)


def test_config_defaults_and_validated_environment_overrides():
    assert ActionSequenceConfig.from_environ({}) == ActionSequenceConfig(
        max_actions=3,
        settle_ms=1_000,
    )
    assert ActionSequenceConfig.from_environ(
        {
            "SAGASU_SEQUENCE_MAX_ACTIONS": "7",
            "SAGASU_SEQUENCE_SETTLE_MS": "2500",
        }
    ) == ActionSequenceConfig(max_actions=7, settle_ms=2_500)

    with pytest.raises(SagasuError) as limit:
        ActionSequenceConfig.from_environ(
            {"SAGASU_SEQUENCE_MAX_ACTIONS": "0"}
        )
    assert limit.value.code == "invalid_arguments"

    with pytest.raises(SagasuError) as delay:
        ActionSequenceConfig.from_environ(
            {"SAGASU_SEQUENCE_SETTLE_MS": "slow"}
        )
    assert delay.value.code == "invalid_arguments"


def test_parser_normalizes_defaults_and_unicode():
    actions = parse_action_sequence(
        document(
            {"operation": "cursor.click", "x": 10, "y": 20},
            {"operation": "text.insert", "text": "有線 IEM"},
        ),
        max_actions=3,
    )

    assert actions[0] == CursorClick(
        operation="cursor.click",
        x=10,
        y=20,
        button="left",
        count=1,
        hold_ms=0,
        backend="humancursor",
    )
    encoded = encode_action_sequence(actions)
    assert "有線 IEM" in encoded
    assert parse_action_sequence(encoded, max_actions=3) == actions

    movement = parse_action_sequence(
        document({"operation": "cursor.move", "x": 1, "y": 2})
    )
    movement_json = encode_action_sequence(movement)
    assert "duration_ms" not in movement_json
    assert parse_action_sequence(movement_json) == movement


@pytest.mark.parametrize(
    ("actions", "code"),
    [
        ([], "invalid_arguments"),
        (
            [
                {"operation": "cursor.move", "x": 1, "y": 2},
                {"operation": "cursor.move", "x": 2, "y": 3},
            ],
            "sequence_too_long",
        ),
        ([{"operation": "screenshot"}], "invalid_arguments"),
        ([{"operation": "dom"}], "invalid_arguments"),
        (
            [{"operation": "cursor.move", "x": True, "y": 2}],
            "invalid_arguments",
        ),
        (
            [{"operation": "cursor.move", "x": 1, "y": 2, "extra": 3}],
            "invalid_arguments",
        ),
    ],
)
def test_parser_rejects_unqueueable_or_invalid_actions(actions, code):
    with pytest.raises(SagasuError) as error:
        parse_action_sequence(json.dumps(actions), max_actions=1)
    assert error.value.code == code


def test_navigation_must_be_final():
    with pytest.raises(SagasuError) as error:
        parse_action_sequence(
            document(
                {"operation": "page.navigate", "url": "https://example.test"},
                {"operation": "cursor.move", "x": 1, "y": 2},
            )
        )
    assert error.value.code == "invalid_arguments"

    actions = parse_action_sequence(
        document(
            {"operation": "cursor.move", "x": 1, "y": 2},
            {"operation": "page.navigate", "url": "https://example.test"},
        )
    )
    assert isinstance(actions[-1], PageNavigate)


def test_all_coordinates_are_checked_before_execution():
    actions = parse_action_sequence(
        document(
            {"operation": "cursor.click", "x": 10, "y": 20},
            {"operation": "cursor.scroll", "x": 100, "y": 20, "steps": -2},
        )
    )
    with pytest.raises(SagasuError) as error:
        validate_sequence_coordinates(actions, DisplaySize(100, 80))
    assert error.value.code == "invalid_coordinate"
    assert error.value.message.startswith("action 1 coordinate")


def test_mixed_sequence_runs_in_order_and_reuses_cursor_backend():
    actions = parse_action_sequence(
        document(
            {"operation": "cursor.click", "x": 10, "y": 20},
            {"operation": "text.insert", "text": "apples"},
            {"operation": "cursor.scroll", "x": 30, "y": 40, "steps": -2},
        )
    )
    events = []
    created = []

    def backend_factory(name):
        created.append(name)
        return FakeBackend(events)

    def insert_text(text):
        events.append(("insert", text))
        return TextInsertionResult(
            target_id="target-1",
            title="Search",
            url="https://example.test/",
            character_count=len(text),
            byte_count=len(text.encode()),
        )

    outcome = run_action_sequence(
        actions,
        DisplaySize(100, 80),
        backend_factory=backend_factory,
        pointer_position=lambda: PointerPosition(30, 40),
        insert_text=insert_text,
    )

    assert outcome.completed is True
    assert created == ["humancursor"]
    assert events == [
        ("click", 10, 20, "left", 1, 0.0),
        ("insert", "apples"),
        ("scroll", 30, 40, -2),
    ]
    assert [item["operation"] for item in outcome.results] == [
        "cursor.click",
        "text.insert",
        "cursor.scroll",
    ]
    assert "text" not in outcome.results[1]


def test_runtime_failure_stops_remaining_actions():
    actions = parse_action_sequence(
        document(
            {"operation": "cursor.move", "x": 1, "y": 2},
            {"operation": "cursor.click", "x": 3, "y": 4},
            {"operation": "cursor.scroll", "x": 5, "y": 6, "steps": -1},
        )
    )
    events = []

    class FailingBackend(FakeBackend):
        def click(self, x, y, *, button, count, hold):
            del x, y, button, count, hold
            raise SagasuError("input_failed", "click failed")

    outcome = run_action_sequence(
        actions,
        DisplaySize(100, 80),
        backend_factory=lambda name: FailingBackend(events),
        pointer_position=lambda: PointerPosition(1, 2),
    )

    assert outcome.completed is False
    assert outcome.failed_index == 1
    assert outcome.failure is not None
    assert outcome.failure.code == "input_failed"
    assert len(outcome.results) == 1
    assert events == [("move", 1, 2, None, False)]
