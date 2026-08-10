from __future__ import annotations

import json
import io
import subprocess
from uuid import UUID

import pytest

from sagasu.protocol import SagasuError
from sagasu.sessions.docker import DockerCLI
from sagasu.sessions.models import ContainerSummary, SESSION_LABEL
from sagasu.sessions.resolver import normalize_session_id, resolve_session


SESSION_ID = "6f1c908d-2acc-4a1e-85f6-0f1b96857672"


class FakeDocker:
    def __init__(self, containers=(), inspected=None):
        self.containers = list(containers)
        self.inspected = inspected
        self.queries: list[str] = []

    def containers_for_session(self, session_id):
        self.queries.append(session_id)
        return self.containers

    def inspect_container(self, name):
        self.queries.append(name)
        if isinstance(self.inspected, Exception):
            raise self.inspected
        return self.inspected


def summary(container_id: str, state: str = "running") -> ContainerSummary:
    return ContainerSummary(
        container_id=container_id,
        name=f"sagasu-{container_id}",
        state=state,
        labels={SESSION_LABEL: SESSION_ID},
    )


def test_normalize_session_requires_uuid4():
    assert normalize_session_id(SESSION_ID) == SESSION_ID
    with pytest.raises(SagasuError, match="UUID4") as invalid:
        normalize_session_id("not-a-uuid")
    assert invalid.value.code == "invalid_session"
    with pytest.raises(SagasuError) as wrong_version:
        normalize_session_id(str(UUID("d9428888-122b-11e1-b85c-61cd3cbb3210")))
    assert wrong_version.value.code == "invalid_session"


def test_resolver_reports_missing_duplicate_and_stopped_sessions():
    with pytest.raises(SagasuError) as missing:
        resolve_session(FakeDocker(), session_id=SESSION_ID)
    assert missing.value.code == "session_not_found"

    with pytest.raises(SagasuError) as duplicate:
        resolve_session(
            FakeDocker([summary("one"), summary("two")]),
            session_id=SESSION_ID,
        )
    assert duplicate.value.code == "session_ambiguous"

    with pytest.raises(SagasuError) as stopped:
        resolve_session(
            FakeDocker([summary("one", "exited")]),
            session_id=SESSION_ID,
        )
    assert stopped.value.code == "session_not_running"


def test_resolver_selects_only_running_match():
    resolved = resolve_session(
        FakeDocker([summary("old", "exited"), summary("live")]),
        session_id=SESSION_ID.upper(),
    )
    assert resolved.session_id == SESSION_ID
    assert resolved.container_id == "live"


def test_explicit_container_requires_running_sagasu_container():
    with pytest.raises(SagasuError) as stopped:
        resolve_session(
            FakeDocker(inspected={"State": {"Running": False}}),
            container="preview",
        )
    assert stopped.value.code == "session_not_running"

    with pytest.raises(SagasuError) as foreign:
        resolve_session(
            FakeDocker(
                inspected={
                    "Id": "abc",
                    "Name": "/preview",
                    "State": {"Running": True},
                    "Config": {"Labels": {"other": "value"}},
                }
            ),
            container="preview",
        )
    assert foreign.value.code == "not_sagasu_container"


def test_explicit_preview_can_have_no_session_label():
    resolved = resolve_session(
        FakeDocker(
            inspected={
                "Id": "abc123",
                "Name": "/sagasu-preview",
                "State": {"Running": True},
                "Config": {
                    "Labels": {"computer.sagasu.browser": "helium"}
                },
            }
        ),
        container="sagasu-preview",
    )
    assert resolved.session_id is None
    assert resolved.container_id == "abc123"
    assert resolved.container_name == "sagasu-preview"


def test_docker_listing_uses_label_filter_and_argument_array():
    calls = []

    def runner(command, **kwargs):
        calls.append((command, kwargs))
        line = {
            "ID": "abc",
            "Names": "session",
            "State": "running",
            "Labels": f"{SESSION_LABEL}={SESSION_ID}",
        }
        return subprocess.CompletedProcess(
            command,
            0,
            stdout=(json.dumps(line) + "\n").encode(),
            stderr=b"",
        )

    docker = DockerCLI(runner=runner)
    result = docker.containers_for_session(SESSION_ID)
    assert result[0].container_id == "abc"
    command = calls[0][0]
    assert command[:3] == ["docker", "container", "ls"]
    assert f"label={SESSION_LABEL}={SESSION_ID}" in command
    assert calls[0][1]["stdin"] is subprocess.DEVNULL


def test_docker_exec_is_non_tty_and_runs_as_sagasu():
    calls = []
    response = {
        "ok": True,
        "operation": "display",
        "backend": "xdotool",
        "display": {"width": 10, "height": 20},
        "pointer": {"x": 1, "y": 2},
    }

    def runner(command, **kwargs):
        calls.append(command)
        return subprocess.CompletedProcess(
            command, 0, stdout=json.dumps(response).encode(), stderr=b""
        )

    DockerCLI(runner=runner).exec_json("container-id", ["display"])
    command = calls[0]
    assert command == [
        "docker",
        "exec",
        "--user",
        "sagasu",
        "container-id",
        "sagasu-session-exec",
        "display",
    ]
    assert "-t" not in command
    assert "-i" not in command


def test_docker_stream_json_keeps_content_and_metadata_separate():
    calls = []
    metadata = {
        "ok": True,
        "operation": "dom.fetch",
        "backend": "cdp",
    }

    class Process:
        returncode = 0

        def communicate(self):
            return None, ("warning\n" + json.dumps(metadata) + "\n").encode()

    def popen(command, **kwargs):
        calls.append((command, kwargs))
        kwargs["stdout"].write(b"<html></html>")
        return Process()

    output = io.BytesIO()
    payload = DockerCLI(popen=popen).exec_stream_json(
        "container-id",
        ["dom"],
        output,
    )
    assert output.getvalue() == b"<html></html>"
    assert payload == metadata
    assert calls[0][0][-2:] == ["sagasu-session-exec", "dom"]
    assert calls[0][1]["stdin"] is subprocess.DEVNULL


def test_docker_sequence_forwards_large_document_on_stdin_not_argv():
    calls = []
    communicated = []
    action_document = (
        b'[{"operation":"text.insert","text":"'
        + b"a" * (64 * 1024)
        + b'"},{"operation":"text.insert","text":"'
        + b"b" * (64 * 1024)
        + b'"}]'
    )
    metadata = {"ok": True, "operation": "actions.sequence"}

    class Process:
        returncode = 0

        def communicate(self, input=None):
            communicated.append(input)
            return None, (json.dumps(metadata) + "\n").encode()

    def popen(command, **kwargs):
        calls.append((command, kwargs))
        kwargs["stdout"].write(b"PNG")
        return Process()

    output = io.BytesIO()
    payload = DockerCLI(popen=popen).exec_stream_json(
        "container-id",
        ["sequence", "--settle-ms", "1000"],
        output,
        input_data=action_document,
    )

    command, options = calls[0]
    assert payload == metadata
    assert output.getvalue() == b"PNG"
    assert len(action_document) > 131_072
    assert command[-3:] == ["sequence", "--settle-ms", "1000"]
    assert "--interactive" in command
    assert all("a" * 1_000 not in argument for argument in command)
    assert options["stdin"] is subprocess.PIPE
    assert communicated == [action_document]
