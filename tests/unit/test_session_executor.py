from __future__ import annotations

import fcntl
import io
import json
import os
import subprocess
from contextlib import contextmanager

import pytest

from sagasu.cdp.dom import DOMSnapshot
from sagasu.cdp.insert_text import TextInsertionResult
from sagasu.cdp.locate import ElementLocation
from sagasu.cdp.navigate import NavigationResult
from sagasu.cli import session_executor as cli
from sagasu.cli.action_sequence import SequenceExecution
from sagasu.protocol import SagasuError
from sagasu.sessions.activity import paths_for_lock, read_activity
from sagasu.sessions.locking import session_lock
from sagasu.xcontrol.cursor import XDoToolBackend, create_backend
from sagasu.xcontrol.display import DisplaySize, PointerPosition


class FakeBackend:
    name = "fake"

    def __init__(self):
        self.calls = []

    def move(self, *args, **kwargs):
        self.calls.append(("move", args, kwargs))

    def click(self, *args, **kwargs):
        self.calls.append(("click", args, kwargs))

    def drag(self, *args, **kwargs):
        self.calls.append(("drag", args, kwargs))

    def scroll(self, *args, **kwargs):
        self.calls.append(("scroll", args, kwargs))


def install_fake_display(monkeypatch):
    monkeypatch.setattr(cli, "get_display_size", lambda: DisplaySize(100, 80))
    monkeypatch.setattr(
        cli, "get_pointer_position", lambda: PointerPosition(12, 13)
    )


def execute(monkeypatch, tmp_path, argv):
    install_fake_display(monkeypatch)
    backend = FakeBackend()
    selected = []

    def backend_factory(name):
        selected.append(name)
        return backend

    monkeypatch.setattr(cli, "create_backend", backend_factory)
    output = io.StringIO()
    arguments = cli.build_parser().parse_args(argv)
    cli.execute(
        arguments,
        text_stdout=output,
        lock_path=tmp_path / "control.lock",
        pause_path=tmp_path / "paused",
    )
    return json.loads(output.getvalue()), backend, selected


def test_humancursor_is_default_and_result_has_geometry(monkeypatch, tmp_path):
    payload, backend, selected = execute(
        monkeypatch,
        tmp_path,
        ["cursor", "move", "20", "30", "--duration-ms", "250"],
    )
    assert selected == ["humancursor"]
    assert backend.calls == [
        (
            "move",
            (20, 30),
            {"duration": 0.25, "steady": False},
        )
    ]
    assert payload == {
        "ok": True,
        "operation": "cursor.move",
        "backend": "humancursor",
        "display": {"width": 100, "height": 80},
        "pointer": {"x": 12, "y": 13},
    }


def test_xdotool_must_be_selected_explicitly(monkeypatch, tmp_path):
    payload, _, selected = execute(
        monkeypatch,
        tmp_path,
        ["cursor", "move", "20", "30", "--backend", "xdotool"],
    )
    assert selected == ["xdotool"]
    assert payload["backend"] == "xdotool"


def test_out_of_bounds_is_rejected_before_using_prepared_backend(
    monkeypatch, tmp_path
):
    install_fake_display(monkeypatch)
    backend = FakeBackend()
    monkeypatch.setattr(cli, "create_backend", lambda name: backend)
    arguments = cli.build_parser().parse_args(["cursor", "move", "100", "2"])
    with pytest.raises(SagasuError) as error:
        cli.execute(
            arguments,
            text_stdout=io.StringIO(),
            lock_path=tmp_path / "control.lock",
            pause_path=tmp_path / "paused",
        )
    assert error.value.code == "invalid_coordinate"
    assert backend.calls == []


def test_pause_starting_during_backend_setup_blocks_cursor_action(
    monkeypatch, tmp_path
):
    install_fake_display(monkeypatch)
    paused = tmp_path / "paused"
    backend = FakeBackend()

    def backend_factory(name):
        del name
        paused.write_text("paused\n")
        return backend

    monkeypatch.setattr(cli, "create_backend", backend_factory)
    arguments = cli.build_parser().parse_args(["cursor", "move", "20", "30"])

    with pytest.raises(SagasuError) as error:
        cli.execute(
            arguments,
            text_stdout=io.StringIO(),
            lock_path=tmp_path / "control.lock",
            pause_path=paused,
        )

    assert error.value.code == "human_control"
    assert backend.calls == []


def test_pause_blocks_mutation_but_not_observation(monkeypatch, tmp_path):
    install_fake_display(monkeypatch)
    paused = tmp_path / "paused"
    paused.write_text("paused\n")
    arguments = cli.build_parser().parse_args(["cursor", "move", "1", "2"])
    with pytest.raises(SagasuError) as error:
        cli.execute(
            arguments,
            text_stdout=io.StringIO(),
            lock_path=tmp_path / "control.lock",
            pause_path=paused,
        )
    assert error.value.code == "human_control"

    output = io.StringIO()
    display = cli.build_parser().parse_args(["display"])
    cli.execute(
        display,
        text_stdout=output,
        lock_path=tmp_path / "control.lock",
        pause_path=paused,
    )
    assert json.loads(output.getvalue())["operation"] == "display"


def test_dom_streams_html_and_reports_cdp_metadata(monkeypatch, tmp_path):
    install_fake_display(monkeypatch)

    def stream(destination):
        html = "<html><body>live</body></html>"
        destination.write(html.encode())
        return DOMSnapshot(
            html=html,
            target_id="target-1",
            title="Live page",
            url="https://example.test/",
            byte_count=len(html),
        )

    monkeypatch.setattr(cli, "stream_active_dom", stream)
    binary = io.BytesIO()
    metadata = io.StringIO()
    arguments = cli.build_parser().parse_args(["dom"])
    cli.execute(
        arguments,
        binary_stdout=binary,
        metadata_stream=metadata,
        lock_path=tmp_path / "control.lock",
        pause_path=tmp_path / "paused",
    )
    assert binary.getvalue() == b"<html><body>live</body></html>"
    payload = json.loads(metadata.getvalue())
    assert payload["operation"] == "dom.fetch"
    assert payload["backend"] == "cdp"
    assert payload["target_id"] == "target-1"
    assert payload["display"] == {"width": 100, "height": 80}


def test_cdp_navigation_and_text_insertion_report_metadata(
    monkeypatch, tmp_path
):
    install_fake_display(monkeypatch)
    monkeypatch.setattr(
        cli,
        "navigate_active_page",
        lambda url: NavigationResult(
            target_id="target-1",
            requested_url=url,
            frame_id="frame-1",
            loader_id="loader-1",
            is_download=False,
        ),
    )
    inserted = []

    def insert(text):
        inserted.append(text)
        return TextInsertionResult(
            target_id="target-1",
            title="Results",
            url="https://example.test/results",
            character_count=len(text),
            byte_count=len(text.encode("utf-8")),
        )

    monkeypatch.setattr(cli, "insert_text_active_page", insert)

    navigation_output = io.StringIO()
    navigation = cli.build_parser().parse_args(
        ["navigate", "https://example.test/results"]
    )
    cli.execute(
        navigation,
        text_stdout=navigation_output,
        lock_path=tmp_path / "control.lock",
        pause_path=tmp_path / "paused",
    )
    navigation_payload = json.loads(navigation_output.getvalue())
    assert navigation_payload["operation"] == "page.navigate"
    assert navigation_payload["backend"] == "cdp"
    assert navigation_payload["requested_url"] == (
        "https://example.test/results"
    )
    assert navigation_payload["loader_id"] == "loader-1"

    text_output = io.StringIO()
    text = cli.build_parser().parse_args(["insert-text", "有線 IEM"])
    cli.execute(
        text,
        text_stdout=text_output,
        lock_path=tmp_path / "control.lock",
        pause_path=tmp_path / "paused",
    )
    text_payload = json.loads(text_output.getvalue())
    assert inserted == ["有線 IEM"]
    assert text_payload["operation"] == "text.insert"
    assert text_payload["backend"] == "cdp"
    assert text_payload["characters"] == len("有線 IEM")
    assert "text" not in text_payload


def test_cdp_element_location_reports_absolute_screen_point(
    monkeypatch, tmp_path
):
    install_fake_display(monkeypatch)
    monkeypatch.setattr(
        cli,
        "locate_active_element",
        lambda selector, **kwargs: ElementLocation(
            target_id="target-1",
            title="Results",
            url="https://example.test/results",
            selector=selector,
            node_id=73,
            screen_x=40,
            screen_y=50,
            viewport_x=40,
            viewport_y=30,
            viewport_width=100,
            viewport_height=60,
            viewport_quad=(30, 20, 50, 20, 50, 40, 30, 40),
            visible_polygon=((30, 20), (50, 20), (50, 40), (30, 40)),
            viewport_origin_x=0,
            viewport_origin_y=20,
            scale_x=1,
            scale_y=1,
            window_left=0,
            window_top=0,
            window_width=100,
            window_height=80,
        ),
    )

    output = io.StringIO()
    arguments = cli.build_parser().parse_args(["locate", "button.buy"])
    cli.execute(
        arguments,
        text_stdout=output,
        lock_path=tmp_path / "control.lock",
        pause_path=tmp_path / "paused",
    )

    payload = json.loads(output.getvalue())
    assert payload["operation"] == "element.locate"
    assert payload["backend"] == "cdp"
    assert payload["selector"] == "button.buy"
    assert payload["screen"] == {"x": 40, "y": 50}
    assert payload["viewport"]["point"] == {"x": 40, "y": 30}
    assert payload["mapping"]["viewport_origin"] == {"x": 0, "y": 20}


def test_sequence_holds_one_lock_through_delay_and_screenshot(
    monkeypatch, tmp_path
):
    install_fake_display(monkeypatch)
    locked = False
    events = []
    backend = FakeBackend()

    @contextmanager
    def tracking_lock(*, exclusive, path):
        nonlocal locked
        del path
        assert exclusive is True
        assert locked is False
        locked = True
        events.append("lock.enter")
        try:
            yield
        finally:
            events.append("lock.exit")
            locked = False

    def create_backend(name):
        assert locked is False
        events.append(("backend.create", name))
        return backend

    def run(actions, display, *, backend_factory):
        assert locked is True
        assert backend_factory("humancursor") is backend
        events.append(("run", len(actions), display))
        return SequenceExecution(
            (
                {
                    "ok": True,
                    "index": 0,
                    "operation": "cursor.move",
                    "backend": "humancursor",
                    "display": {"width": 100, "height": 80},
                    "pointer": {"x": 12, "y": 13},
                },
            )
        )

    def settle(seconds):
        assert locked is True
        events.append(("sleep", seconds))

    def capture(destination, *, include_pointer):
        assert locked is True
        events.append(("capture", include_pointer))
        destination.write(b"PNG")

    monkeypatch.setattr(cli, "session_lock", tracking_lock)
    monkeypatch.setattr(cli, "create_backend", create_backend)
    monkeypatch.setattr(cli, "run_action_sequence", run)
    monkeypatch.setattr(cli, "stream_png", capture)
    binary = io.BytesIO()
    metadata = io.StringIO()
    arguments = cli.build_parser().parse_args(
        ["sequence"]
    )

    cli.execute(
        arguments,
        binary_stdin=io.BytesIO(
            b'[{"operation":"cursor.move","x":20,"y":30}]'
        ),
        binary_stdout=binary,
        metadata_stream=metadata,
        lock_path=tmp_path / "control.lock",
        pause_path=tmp_path / "paused",
        environ={},
        sleep=settle,
    )

    assert binary.getvalue() == b"PNG"
    assert events == [
        ("backend.create", "humancursor"),
        "lock.enter",
        ("run", 1, DisplaySize(100, 80)),
        ("sleep", 1.0),
        ("capture", True),
        "lock.exit",
    ]
    payload = json.loads(metadata.getvalue())
    assert payload["operation"] == "actions.sequence"
    assert payload["completed"] is True
    assert payload["action_count"] == 1
    assert payload["actions_completed"] == 1
    assert payload["settle_ms"] == 1_000
    assert payload["pointer_included"] is True


def test_failed_sequence_still_waits_and_captures(monkeypatch, tmp_path):
    install_fake_display(monkeypatch)
    sleeps = []
    captures = []
    failure = SagasuError("input_failed", "the click failed", {"backend": "fake"})
    monkeypatch.setattr(
        cli,
        "run_action_sequence",
        lambda actions, display, **kwargs: SequenceExecution((), failure, 0),
    )
    monkeypatch.setattr(cli, "create_backend", lambda name: FakeBackend())
    monkeypatch.setattr(
        cli,
        "stream_png",
        lambda destination, *, include_pointer: (
            captures.append(include_pointer),
            destination.write(b"diagnostic"),
        )[-1],
    )
    binary = io.BytesIO()
    metadata = io.StringIO()
    arguments = cli.build_parser().parse_args(
        [
            "sequence",
            "--settle-ms",
            "250",
            "--no-pointer",
        ]
    )

    cli.execute(
        arguments,
        binary_stdin=io.BytesIO(
            b'[{"operation":"cursor.click","x":20,"y":30}]'
        ),
        binary_stdout=binary,
        metadata_stream=metadata,
        lock_path=tmp_path / "control.lock",
        pause_path=tmp_path / "paused",
        environ={},
        sleep=sleeps.append,
    )

    payload = json.loads(metadata.getvalue())
    assert binary.getvalue() == b"diagnostic"
    assert sleeps == [0.25]
    assert captures == [False]
    assert payload["completed"] is False
    assert payload["failed_index"] == 0
    assert payload["failure"] == {
        "code": "input_failed",
        "message": "the click failed",
        "details": {"backend": "fake"},
    }


@pytest.mark.parametrize("stage", ["screenshot", "pointer"])
@pytest.mark.parametrize("partial_failure", [False, True])
def test_final_observation_failure_preserves_authoritative_sequence_state(
    monkeypatch, tmp_path, stage, partial_failure
):
    install_fake_display(monkeypatch)
    successful_results = tuple(
        {
            "ok": True,
            "index": index,
            "operation": "cursor.move",
            "backend": "humancursor",
            "display": {"width": 100, "height": 80},
            "pointer": {"x": 12, "y": 13},
        }
        for index in range(1 if partial_failure else 2)
    )
    action_failure = SagasuError(
        "input_failed",
        "the second movement failed",
        {"backend": "humancursor"},
    )
    execution = SequenceExecution(
        successful_results,
        action_failure if partial_failure else None,
        1 if partial_failure else None,
    )
    monkeypatch.setattr(
        cli,
        "run_action_sequence",
        lambda actions, display, **kwargs: execution,
    )
    monkeypatch.setattr(cli, "create_backend", lambda name: FakeBackend())

    observation_error = SagasuError(
        "capture_failed" if stage == "screenshot" else "input_failed",
        f"final {stage} observation failed",
        {"reason": "transient"},
    )

    def capture(destination, *, include_pointer):
        del include_pointer
        destination.write(b"partial" if stage == "screenshot" else b"PNG")
        if stage == "screenshot":
            raise observation_error

    def pointer_position():
        if stage == "pointer":
            raise observation_error
        pytest.fail("the final pointer query must not follow a failed screenshot")

    monkeypatch.setattr(cli, "stream_png", capture)
    monkeypatch.setattr(cli, "get_pointer_position", pointer_position)
    binary = io.BytesIO()
    metadata = io.StringIO()
    arguments = cli.build_parser().parse_args(
        ["sequence", "--settle-ms", "25", "--no-pointer"]
    )

    with pytest.raises(SagasuError) as raised:
        cli.execute(
            arguments,
            binary_stdin=io.BytesIO(
                b'[{"operation":"cursor.move","x":20,"y":30},'
                b'{"operation":"cursor.move","x":40,"y":50}]'
            ),
            binary_stdout=binary,
            metadata_stream=metadata,
            lock_path=tmp_path / "control.lock",
            pause_path=tmp_path / "paused",
            environ={},
            sleep=lambda seconds: None,
        )

    assert raised.value.code == observation_error.code
    assert raised.value.details["reason"] == "transient"
    state = raised.value.details["sequence_state"]
    assert state["completed"] is not partial_failure
    assert state["action_count"] == 2
    assert state["actions_completed"] == len(successful_results)
    assert state["results"] == list(successful_results)
    assert state["display"] == {"width": 100, "height": 80}
    assert state["settle_ms"] == 25
    assert state["settle_completed"] is True
    assert state["pointer_included"] is False
    assert state["screenshot_captured"] is (stage == "pointer")
    assert state["pointer_observed"] is False
    assert state["observation_stage"] == stage
    if partial_failure:
        assert state["failed_index"] == 1
        assert state["failure"] == action_failure.as_dict()["error"]
    else:
        assert "failed_index" not in state
        assert "failure" not in state
    assert binary.getvalue() == (
        b"partial" if stage == "screenshot" else b"PNG"
    )
    assert metadata.getvalue() == ""
    assert raised.value.as_dict()["error"]["details"]["sequence_state"] == state


def test_sequence_limit_is_container_enforced_before_actions(
    monkeypatch, tmp_path
):
    install_fake_display(monkeypatch)
    monkeypatch.setattr(
        cli,
        "run_action_sequence",
        lambda actions, display: pytest.fail("actions must not run"),
    )
    actions = [
        {"operation": "cursor.move", "x": index, "y": index}
        for index in range(4)
    ]
    arguments = cli.build_parser().parse_args(["sequence"])

    with pytest.raises(SagasuError) as error:
        cli.execute(
            arguments,
            binary_stdin=io.BytesIO(json.dumps(actions).encode()),
            binary_stdout=io.BytesIO(),
            metadata_stream=io.StringIO(),
            lock_path=tmp_path / "control.lock",
            pause_path=tmp_path / "paused",
            environ={"SAGASU_SEQUENCE_MAX_ACTIONS": "3"},
            sleep=lambda seconds: None,
        )
    assert error.value.code == "sequence_too_long"


@pytest.mark.parametrize("input_data", (b"", b"{"))
def test_missing_or_malformed_sequence_stdin_is_structured(
    monkeypatch, capsys, input_data
):
    @contextmanager
    def no_activity(**kwargs):
        del kwargs
        yield

    monkeypatch.setattr(cli, "agent_activity", no_activity)

    status = cli.main(
        ["sequence"],
        binary_stdin=io.BytesIO(input_data),
    )

    captured = capsys.readouterr()
    assert status == 2
    assert captured.out == ""
    payload = json.loads(captured.err)
    assert payload["ok"] is False
    assert payload["error"]["code"] == "invalid_arguments"
    assert payload["error"]["exit_status"] == 2


def test_sequence_stdin_is_bounded_and_structured(monkeypatch, capsys):
    @contextmanager
    def no_activity(**kwargs):
        del kwargs
        yield

    monkeypatch.setattr(cli, "agent_activity", no_activity)
    monkeypatch.setattr(cli, "MAX_SEQUENCE_INPUT_BYTES", 8)
    monkeypatch.setattr(cli, "SEQUENCE_INPUT_CHUNK_BYTES", 3)

    status = cli.main(
        ["sequence"],
        binary_stdin=io.BytesIO(b"123456789"),
    )

    captured = capsys.readouterr()
    assert status == 2
    payload = json.loads(captured.err)
    assert payload["error"]["code"] == "invalid_arguments"
    assert payload["error"]["details"] == {
        "bytes_at_least": 9,
        "max_bytes": 8,
    }


def test_human_pause_blocks_sequence_before_actions(monkeypatch, tmp_path):
    install_fake_display(monkeypatch)
    paused = tmp_path / "paused"
    paused.write_text("paused\n")
    monkeypatch.setattr(
        cli,
        "run_action_sequence",
        lambda actions, display: pytest.fail("actions must not run"),
    )
    arguments = cli.build_parser().parse_args(["sequence"])

    with pytest.raises(SagasuError) as error:
        cli.execute(
            arguments,
            binary_stdin=io.BytesIO(
                b'[{"operation":"cursor.move","x":20,"y":30}]'
            ),
            binary_stdout=io.BytesIO(),
            metadata_stream=io.StringIO(),
            lock_path=tmp_path / "control.lock",
            pause_path=paused,
            environ={},
            sleep=lambda seconds: None,
        )
    assert error.value.code == "human_control"


@pytest.mark.parametrize(
    "argv",
    [
        ["navigate", "https://example.test/"],
        ["insert-text", "query"],
    ],
)
def test_pause_blocks_cdp_mutations(monkeypatch, tmp_path, argv):
    install_fake_display(monkeypatch)
    paused = tmp_path / "paused"
    paused.write_text("paused\n")
    monkeypatch.setattr(
        cli,
        "navigate_active_page",
        lambda url: pytest.fail("CDP must not be called"),
    )
    monkeypatch.setattr(
        cli,
        "insert_text_active_page",
        lambda text: pytest.fail("CDP must not be called"),
    )
    arguments = cli.build_parser().parse_args(argv)
    with pytest.raises(SagasuError) as error:
        cli.execute(
            arguments,
            text_stdout=io.StringIO(),
            lock_path=tmp_path / "control.lock",
            pause_path=paused,
        )
    assert error.value.code == "human_control"


def test_click_drag_scroll_validate_current_and_options(monkeypatch, tmp_path):
    payload, backend, _ = execute(
        monkeypatch,
        tmp_path,
        [
            "cursor",
            "click",
            "--current",
            "--button",
            "right",
            "--count",
            "2",
            "--hold-ms",
            "30",
        ],
    )
    assert payload["operation"] == "cursor.click"
    assert backend.calls[0] == (
        "click",
        (12, 13),
        {"button": "right", "count": 2, "hold": 0.03},
    )

    with pytest.raises(SagasuError) as drag:
        execute(
            monkeypatch,
            tmp_path,
            ["cursor", "drag", "--current", "10"],
        )
    assert drag.value.code == "invalid_arguments"

    with pytest.raises(SagasuError) as scroll:
        execute(
            monkeypatch,
            tmp_path,
            ["cursor", "scroll", "1", "2", "--steps", "0"],
        )
    assert scroll.value.code == "invalid_arguments"


def test_exclusive_lock_fails_immediately_when_occupied(tmp_path):
    lock_path = tmp_path / "control.lock"
    descriptor = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        with pytest.raises(SagasuError) as error:
            with session_lock(exclusive=True, path=lock_path):
                pass
        assert error.value.code == "session_busy"
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def test_shared_locks_allow_parallel_readers(tmp_path):
    lock_path = tmp_path / "control.lock"
    with session_lock(exclusive=False, path=lock_path):
        with session_lock(exclusive=False, path=lock_path):
            pass


def test_every_executor_command_records_idle_activity(monkeypatch, tmp_path):
    install_fake_display(monkeypatch)
    lock_path = tmp_path / "control.lock"
    activity_path, _ = paths_for_lock(lock_path)
    arguments = cli.build_parser().parse_args(["display"])

    cli.execute(
        arguments,
        text_stdout=io.StringIO(),
        lock_path=lock_path,
        pause_path=tmp_path / "paused",
    )

    # Entering and leaving the command each advance the epoch.
    assert read_activity(activity_path) >= 2


def test_xdotool_scroll_direction_and_no_human_fallback():
    calls = []

    def runner(command, **kwargs):
        calls.append(command)
        return subprocess.CompletedProcess(command, 0, stdout=b"", stderr=b"")

    backend = XDoToolBackend(runner=runner, sleep=lambda _: None)
    backend.scroll(5, 6, steps=3)
    assert calls[-1][-1] == "4"
    backend.scroll(5, 6, steps=-2)
    assert calls[-1][-1] == "5"

    assert isinstance(create_backend("xdotool"), XDoToolBackend)
    with pytest.raises(SagasuError):
        create_backend("automatic")
