from __future__ import annotations

import json

import pytest

from sagasu.cli import session as session_cli
from sagasu.cli.main import build_parser, main
from sagasu.cli.session import _runtime_arguments, _sequence_invocation
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


def test_sequence_invocation_separates_validated_actions_from_argv():
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

    runtime, input_data = _sequence_invocation(parsed)
    assert runtime == ["sequence", "--settle-ms", "2500", "--no-pointer"]
    assert json.loads(input_data) == [
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
    assert "有線 IEM" not in " ".join(runtime)


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

    def save(
        executor,
        destination,
        *,
        executor_arguments,
        executor_input,
        overwrite,
    ):
        captured.update(
            {
                "executor": executor,
                "destination": destination,
                "arguments": executor_arguments,
                "input": executor_input,
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
    assert captured["arguments"] == ["sequence"]
    assert json.loads(captured["input"]) == [
        {
            "operation": "cursor.move",
            "x": 10,
            "y": 20,
            "steady": False,
            "backend": "humancursor",
        }
    ]
    assert captured["overwrite"] is True


def test_large_valid_sequence_is_forwarded_only_through_stdin(monkeypatch):
    text_a = "a" * (64 * 1024)
    text_b = "b" * (64 * 1024)
    parsed = build_parser().parse_args(
        [
            "session",
            "sequence",
            SESSION_ID,
            "--actions-json",
            json.dumps(
                [
                    {"operation": "text.insert", "text": text_a},
                    {"operation": "text.insert", "text": text_b},
                ]
            ),
            "--out",
            "screen.png",
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

    def save(
        executor,
        destination,
        *,
        executor_arguments,
        executor_input,
        overwrite,
    ):
        del executor, destination, overwrite
        captured["arguments"] = executor_arguments
        captured["input"] = executor_input
        return {"ok": True, "operation": "actions.sequence"}

    monkeypatch.setattr(session_cli, "save_action_sequence_screenshot", save)

    session_cli.run(parsed, object())

    assert captured["arguments"] == ["sequence"]
    assert len(captured["input"]) > 131_072
    assert json.loads(captured["input"])[0]["text"] == text_a
    assert text_a not in captured["arguments"]
    assert text_b not in captured["arguments"]


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


@pytest.mark.parametrize(
    "argv",
    [
        ["session", "insert-text", SESSION_ID, "\ud800"],
        [
            "session",
            "navigate",
            SESSION_ID,
            "https://example.test/\udfff",
        ],
        [
            "session",
            "sequence",
            SESSION_ID,
            "--actions-json",
            r'[{"operation":"text.insert","text":"\ud800"}]',
            "--out",
            "screen.png",
        ],
    ],
)
def test_cli_rejects_lone_unicode_surrogates_as_invalid_arguments(
    argv, capsys
):
    status = main(argv)

    captured = capsys.readouterr()
    assert status == 2
    assert captured.out == ""
    payload = json.loads(captured.err)
    assert payload["error"]["code"] == "invalid_arguments"
    assert payload["error"]["exit_status"] == 2
