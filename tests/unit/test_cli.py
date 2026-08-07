from __future__ import annotations

import json

import pytest

from sagasu.cli import session as session_cli
from sagasu.cli.main import build_parser
from sagasu.cli.session import _runtime_arguments
from sagasu.protocol import SagasuError
from sagasu.sessions.models import ResolvedSession


SESSION_ID = "6f1c908d-2acc-4a1e-85f6-0f1b96857672"


@pytest.mark.parametrize(
    ("argv", "command"),
    [
        (["session", "display", SESSION_ID], "display"),
        (
            ["session", "screenshot", SESSION_ID, "--out", "screen.png"],
            "screenshot",
        ),
        (
            ["session", "dom", SESSION_ID, "--out", "page.html"],
            "dom",
        ),
        (
            ["session", "locate", SESSION_ID, "button.buy"],
            "locate",
        ),
        (
            [
                "session",
                "navigate",
                SESSION_ID,
                "https://example.test/",
            ],
            "navigate",
        ),
        (
            ["session", "insert-text", SESSION_ID, "有線 IEM"],
            "insert-text",
        ),
        (
            [
                "session",
                "sequence",
                SESSION_ID,
                "--actions-json",
                '[{"operation":"cursor.move","x":10,"y":20}]',
                "--out",
                "screen.png",
            ],
            "sequence",
        ),
        (
            ["session", "cursor", SESSION_ID, "position"],
            "cursor",
        ),
        (
            [
                "session",
                "cursor",
                "--container",
                "sagasu-preview",
                "click",
                "10",
                "20",
            ],
            "cursor",
        ),
    ],
)
def test_public_command_shapes_parse(argv, command):
    assert build_parser().parse_args(argv).session_command == command


def test_container_replaces_session_but_cannot_accompany_it():
    parsed = build_parser().parse_args(
        ["session", "display", "--container", "sagasu-preview"]
    )
    assert parsed.session_target is None
    assert parsed.container == "sagasu-preview"

    dom = build_parser().parse_args(
        [
            "session",
            "dom",
            "--container",
            "sagasu-preview",
            "--out",
            "page.html",
        ]
    )
    assert dom.session_target is None
    assert dom.container == "sagasu-preview"

    locate = build_parser().parse_args(
        [
            "session",
            "locate",
            "--container",
            "sagasu-preview",
            "button.buy",
        ]
    )
    assert locate.session_target is None
    assert locate.container == "sagasu-preview"
    assert locate.selector == "button.buy"

    navigate = build_parser().parse_args(
        [
            "session",
            "navigate",
            "--container",
            "sagasu-preview",
            "https://example.test/",
        ]
    )
    assert navigate.session_target is None
    assert navigate.container == "sagasu-preview"
    assert navigate.url == "https://example.test/"

    insert_text = build_parser().parse_args(
        [
            "session",
            "insert-text",
            "--container",
            "sagasu-preview",
            "有線 IEM",
        ]
    )
    assert insert_text.session_target is None
    assert insert_text.container == "sagasu-preview"
    assert insert_text.text == "有線 IEM"

    sequence = build_parser().parse_args(
        [
            "session",
            "sequence",
            "--container",
            "sagasu-preview",
            "--actions-json",
            '[{"operation":"cursor.move","x":10,"y":20}]',
            "--out",
            "screen.png",
        ]
    )
    assert sequence.session_target is None
    assert sequence.container == "sagasu-preview"


@pytest.mark.parametrize("command", ("locate", "navigate", "insert-text"))
def test_explicit_container_does_not_fill_missing_action_operand(command):
    with pytest.raises(SagasuError) as error:
        build_parser().parse_args(
            [
                "session",
                command,
                "--container",
                "sagasu-preview",
            ]
        )

    assert error.value.code == "invalid_arguments"
    assert error.value.exit_status == 2


def test_runtime_arguments_keep_coordinate_action_atomic():
    parsed = build_parser().parse_args(
        [
            "session",
            "cursor",
            SESSION_ID,
            "drag",
            "1",
            "2",
            "30",
            "40",
            "--duration-ms",
            "500",
            "--steady",
        ]
    )
    assert _runtime_arguments(parsed) == [
        "cursor",
        "drag",
        "1",
        "2",
        "30",
        "40",
        "--duration-ms",
        "500",
        "--steady",
        "--backend",
        "humancursor",
    ]


def test_runtime_arguments_expose_supplemental_cdp_actions():
    locate = build_parser().parse_args(
        ["session", "locate", SESSION_ID, "button.buy"]
    )
    assert _runtime_arguments(locate) == [
        "locate",
        "--",
        "button.buy",
    ]

    navigate = build_parser().parse_args(
        [
            "session",
            "navigate",
            SESSION_ID,
            "https://example.test/results?q=iem",
        ]
    )
    assert _runtime_arguments(navigate) == [
        "navigate",
        "--",
        "https://example.test/results?q=iem",
    ]

    insert_text = build_parser().parse_args(
        ["session", "insert-text", SESSION_ID, "有線 IEM"]
    )
    assert _runtime_arguments(insert_text) == [
        "insert-text",
        "--",
        "有線 IEM",
    ]


def test_runtime_arguments_encode_validated_action_sequence():
    parsed = build_parser().parse_args(
        [
            "session",
            "sequence",
            SESSION_ID,
            "--actions-json",
            '[{"operation":"cursor.click","x":10,"y":20},'
            '{"operation":"text.insert","text":"有線 IEM"}]',
            "--settle-ms",
            "2500",
            "--no-pointer",
            "--out",
            "screen.png",
        ]
    )

    runtime = _runtime_arguments(parsed)
    assert runtime[0:2] == ["sequence", "--actions-json"]
    assert json.loads(runtime[2]) == [
        {
            "operation": "cursor.click",
            "x": 10,
            "y": 20,
            "button": "left",
            "count": 1,
            "hold_ms": 0,
            "backend": "humancursor",
        },
        {"operation": "text.insert", "text": "有線 IEM"},
    ]
    assert runtime[3:] == ["--settle-ms", "2500", "--no-pointer"]


def test_sequence_routes_to_streamed_artifact_handler(monkeypatch):
    parsed = build_parser().parse_args(
        [
            "session",
            "sequence",
            SESSION_ID,
            "--actions-json",
            '[{"operation":"cursor.move","x":10,"y":20}]',
            "--out",
            "screen.png",
            "--overwrite",
        ]
    )
    resolved = ResolvedSession(
        session_id=SESSION_ID,
        container_id="container-id",
        container_name="session",
    )
    monkeypatch.setattr(
        session_cli,
        "resolve_session",
        lambda docker, session_id, container: resolved,
    )
    captured = {}

    def save(executor, destination, *, executor_arguments, overwrite):
        captured.update(
            {
                "executor": executor,
                "destination": destination,
                "arguments": executor_arguments,
                "overwrite": overwrite,
            }
        )
        return {"ok": True, "operation": "actions.sequence"}

    monkeypatch.setattr(session_cli, "save_action_sequence_screenshot", save)
    docker = object()

    payload = session_cli.run(parsed, docker)

    assert payload["operation"] == "actions.sequence"
    assert captured["executor"].docker is docker
    assert captured["destination"] == "screen.png"
    assert captured["arguments"][0] == "sequence"
    assert captured["overwrite"] is True


def test_invalid_action_arguments_are_structured():
    parsed = build_parser().parse_args(
        ["session", "cursor", SESSION_ID, "click", "--count", "0", "--current"]
    )
    with pytest.raises(SagasuError) as error:
        _runtime_arguments(parsed)
    assert error.value.code == "invalid_arguments"

    with pytest.raises(SagasuError) as parse_error:
        build_parser().parse_args(
            ["session", "cursor", SESSION_ID, "move", "x", "2"]
        )
    assert parse_error.value.code == "invalid_arguments"

    invalid_url = build_parser().parse_args(
        ["session", "navigate", SESSION_ID, "javascript:alert(1)"]
    )
    with pytest.raises(SagasuError) as navigation:
        _runtime_arguments(invalid_url)
    assert navigation.value.code == "invalid_arguments"
