"""Regression tests for the original defects recorded in ``BUGS.md``."""

from __future__ import annotations

import errno
import io
import json
import subprocess
from contextlib import contextmanager

import pytest

from sagasu.artifacts import atomic
from sagasu.artifacts.html import validate_html
from sagasu.cdp import locate
from sagasu.cdp.coordinates import (
    BrowserWindowBounds,
    CSSScreenSize,
    ViewportMetrics,
    convert_viewport_to_screen,
)
from sagasu.cli import session_executor
from sagasu.cli.main import build_parser
from sagasu.cli.session import _runtime_arguments
from sagasu.protocol import SagasuError
from sagasu.sessions.docker import DockerCLI
from sagasu.xcontrol.display import DisplaySize, PointerPosition


SESSION_ID = "6f1c908d-2acc-4a1e-85f6-0f1b96857672"


def test_bug_1_bottom_chrome_does_not_shift_viewport_origin():
    # The normal page origin is 72 px below the top of this browser window.
    # A 15 px horizontal scrollbar reduces CDP's clientHeight from 695 to 680
    # but does not move the top of the page viewport.
    converted = convert_viewport_to_screen(
        100,
        100,
        window=BrowserWindowBounds(
            left=0,
            top=0,
            width=1365,
            height=767,
        ),
        viewport=ViewportMetrics(
            width=1351,
            height=680,
            zoom=1,
        ),
        css_screen=CSSScreenSize(
            width=1366,
            height=768,
            inner_height=695,
            outer_height=767,
        ),
        display_width=1366,
        display_height=768,
    )

    assert converted.viewport_origin_y == 72
    assert converted.y == 172


def test_bug_2_explicit_container_does_not_fill_a_missing_action_operand():
    with pytest.raises(SagasuError) as error:
        build_parser().parse_args(
            [
                "session",
                "insert-text",
                "--container",
                "sagasu-preview",
            ]
        )

    assert error.value.code == "invalid_arguments"
    assert error.value.exit_status == 2


def test_bug_3_dash_prefixed_text_is_separated_from_executor_options():
    arguments = build_parser().parse_args(
        [
            "session",
            "insert-text",
            SESSION_ID,
            "--",
            "-hello",
        ]
    )

    runtime_arguments = _runtime_arguments(arguments)
    assert runtime_arguments == [
        "insert-text",
        "--",
        "-hello",
    ]
    assert session_executor.build_parser().parse_args(
        runtime_arguments
    ).text == "-hello"


def test_bug_4_executor_parse_failures_remain_json_only(capsys):
    try:
        status = session_executor.main(["insert-text", "-hello"])
    except SystemExit as error:
        pytest.fail(
            f"argparse escaped the executor protocol with status {error.code}"
        )

    captured = capsys.readouterr()
    assert status == 2
    assert captured.out == ""
    payload = json.loads(captured.err)
    assert payload["ok"] is False
    assert payload["error"]["code"] == "invalid_arguments"


def test_bug_5_dom_validation_accepts_non_html_documents(tmp_path):
    documents = {
        "image.svg": (
            '<svg xmlns="http://www.w3.org/2000/svg">'
            '<circle cx="5" cy="5" r="5"/></svg>'
        ),
        "feed.xml": (
            '<?xml version="1.0" encoding="UTF-8"?>'
            "<feed><title>Updates</title></feed>"
        ),
    }

    for name, document in documents.items():
        path = tmp_path / name
        path.write_text(document, encoding="utf-8")
        assert validate_html(path) == len(document.encode("utf-8"))


def test_bug_6_publish_without_overwrite_survives_unsupported_hardlinks(
    monkeypatch, tmp_path
):
    def unsupported_hardlink(source, destination):
        del source, destination
        raise OSError(errno.EOPNOTSUPP, "hard links are not supported")

    monkeypatch.setattr(atomic.os, "link", unsupported_hardlink)
    output = tmp_path / "artifact.bin"

    published = atomic.publish_stream(
        output,
        overwrite=False,
        artifact_name="artifact",
        stream_writer=lambda destination: destination.write(b"captured"),
        validator=lambda path, result: (path.read_bytes(), result),
    )

    assert output.read_bytes() == b"captured"
    assert published.path == output


def test_bug_7_docker_exec_preserves_executor_error_exit_status():
    wire_error = SagasuError(
        "invalid_coordinate",
        "The coordinate is outside the display",
        exit_status=2,
    ).as_dict()

    def runner(command, **kwargs):
        del kwargs
        return subprocess.CompletedProcess(
            command,
            2,
            stdout=b"",
            stderr=(json.dumps(wire_error) + "\n").encode("utf-8"),
        )

    with pytest.raises(SagasuError) as error:
        DockerCLI(runner=runner).exec_json(
            "container-id",
            ["cursor", "click", "5000", "5000"],
        )

    assert error.value.code == "invalid_coordinate"
    assert error.value.exit_status == 2


def test_bug_8_missing_cdp_zoom_defaults_to_unzoomed():
    viewport = locate._viewport_metrics(
        {
            "cssVisualViewport": {
                "clientWidth": 1351,
                "clientHeight": 695,
            }
        }
    )

    assert viewport == ViewportMetrics(width=1351, height=695, zoom=1)


def test_bug_9_non_utf8_docker_listing_is_a_structured_invalid_response():
    def runner(command, **kwargs):
        del kwargs
        return subprocess.CompletedProcess(
            command,
            0,
            stdout=b"\xff\n",
            stderr=b"",
        )

    with pytest.raises(SagasuError) as error:
        DockerCLI(runner=runner).containers_for_session(SESSION_ID)

    assert error.value.code == "invalid_response"
    assert error.value.message == (
        "Docker returned a container listing that is not valid UTF-8"
    )
    assert error.value.details == {
        "encoding": "utf-8",
        "byte_offset": 0,
        "reason": "invalid start byte",
    }


def test_bug_10_cursor_backend_is_created_before_exclusive_lock(
    monkeypatch, tmp_path
):
    events: list[str] = []

    class Backend:
        def move(self, x, y, *, duration, steady):
            del x, y, duration, steady

    @contextmanager
    def tracking_lock(*, exclusive, path):
        del path
        assert exclusive is True
        events.append("lock.enter")
        try:
            yield
        finally:
            events.append("lock.exit")

    def create_backend(name):
        assert name == "humancursor"
        events.append("backend.create")
        return Backend()

    monkeypatch.setattr(session_executor, "session_lock", tracking_lock)
    monkeypatch.setattr(session_executor, "create_backend", create_backend)
    monkeypatch.setattr(
        session_executor,
        "get_display_size",
        lambda: DisplaySize(100, 80),
    )
    monkeypatch.setattr(
        session_executor,
        "get_pointer_position",
        lambda: PointerPosition(10, 20),
    )

    arguments = session_executor.build_parser().parse_args(
        ["cursor", "move", "20", "30"]
    )
    session_executor.execute(
        arguments,
        text_stdout=io.StringIO(),
        lock_path=tmp_path / "control.lock",
        pause_path=tmp_path / "paused",
    )

    assert events.index("backend.create") < events.index("lock.enter")
