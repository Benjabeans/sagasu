"""Host-side access to the private executor in one resolved session."""

from __future__ import annotations

from typing import Any, BinaryIO, Sequence

from sagasu.protocol import SagasuError
from sagasu.sessions.docker import DockerCLI
from sagasu.sessions.models import ResolvedSession


class SessionExecutor:
    """Run private commands and attach host-authoritative session metadata."""

    def __init__(self, docker: DockerCLI, session: ResolvedSession) -> None:
        self.docker = docker
        self.session = session

    def invoke(self, arguments: Sequence[str]) -> dict[str, Any]:
        payload = self.docker.exec_json(self.session.container_id, arguments)
        validate_executor_result(payload)
        return self.authoritative(payload)

    def stream(
        self,
        arguments: Sequence[str],
        destination: BinaryIO,
    ) -> None:
        self.docker.exec_stream(
            self.session.container_id,
            arguments,
            destination,
        )

    def stream_json(
        self,
        arguments: Sequence[str],
        destination: BinaryIO,
    ) -> dict[str, Any]:
        payload = self.docker.exec_stream_json(
            self.session.container_id,
            arguments,
            destination,
        )
        validate_executor_result(payload)
        return self.authoritative(payload)

    def authoritative(self, payload: dict[str, Any]) -> dict[str, Any]:
        # A process inside the browser container cannot authoritatively identify
        # its host-side session or container.
        payload["session_id"] = self.session.session_id
        payload["container_id"] = self.session.container_id
        return payload


def validate_executor_result(payload: dict[str, Any]) -> None:
    required = ("operation", "backend", "display", "pointer")
    missing = [key for key in required if key not in payload]
    display = payload.get("display")
    pointer = payload.get("pointer")
    if missing or not isinstance(display, dict) or not isinstance(pointer, dict):
        raise SagasuError(
            "invalid_response",
            "The session executor returned an incomplete response",
            {"missing": missing},
        )
    if not _integer_pair(display, "width", "height", positive=True):
        raise SagasuError(
            "invalid_response",
            "The session executor returned invalid display dimensions",
        )
    if not _integer_pair(pointer, "x", "y", positive=False):
        raise SagasuError(
            "invalid_response",
            "The session executor returned an invalid pointer position",
        )


def _integer_pair(
    value: dict[str, Any],
    first: str,
    second: str,
    *,
    positive: bool,
) -> bool:
    items = (value.get(first), value.get(second))
    if any(isinstance(item, bool) or not isinstance(item, int) for item in items):
        return False
    if positive:
        return all(item > 0 for item in items)
    return all(item >= 0 for item in items)

