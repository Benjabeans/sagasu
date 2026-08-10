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
        *,
        input_data: bytes | None = None,
        failure_code: str = "dom_failed",
        failure_message: str = "The in-container DOM command failed",
    ) -> dict[str, Any]:
        if input_data is None:
            payload = self.docker.exec_stream_json(
                self.session.container_id,
                arguments,
                destination,
                failure_code=failure_code,
                failure_message=failure_message,
            )
        else:
            payload = self.docker.exec_stream_json(
                self.session.container_id,
                arguments,
                destination,
                input_data=input_data,
                failure_code=failure_code,
                failure_message=failure_message,
            )
        validate_executor_result(payload)
        return self.authoritative(payload)

    def authoritative(self, payload: dict[str, Any]) -> dict[str, Any]:
        # A process inside the browser container cannot authoritatively identify
        # its host-side session or container.
        payload["session_id"] = self.session.session_id
        payload["container_id"] = self.session.container_id
        return payload


def validate_executor_result(
    payload: dict[str, Any],
    *,
    allow_pointer_observation_failure: bool = False,
) -> None:
    required = ("operation", "backend", "display", "pointer")
    missing = [key for key in required if key not in payload]
    display = payload.get("display")
    pointer = payload.get("pointer")
    if missing or not isinstance(display, dict):
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
    if isinstance(pointer, dict):
        if not _integer_pair(pointer, "x", "y", positive=False):
            raise SagasuError(
                "invalid_response",
                "The session executor returned an invalid pointer position",
            )
        if allow_pointer_observation_failure and "pointer_observation" in payload:
            raise SagasuError(
                "invalid_response",
                "The session executor returned contradictory pointer metadata",
            )
        return
    if not (
        allow_pointer_observation_failure
        and pointer is None
        and _valid_pointer_observation_failure(
            payload.get("pointer_observation")
        )
    ):
        raise SagasuError(
            "invalid_response",
            "The session executor returned an invalid pointer position",
        )


def _valid_pointer_observation_failure(value: object) -> bool:
    if not isinstance(value, dict) or set(value) != {"ok", "error"}:
        return False
    if value.get("ok") is not False:
        return False
    error = value.get("error")
    if not isinstance(error, dict) or not set(error) <= {
        "code",
        "message",
        "details",
        "exit_status",
    }:
        return False
    if not all(
        isinstance(error.get(key), str) and error.get(key)
        for key in ("code", "message")
    ):
        return False
    details = error.get("details")
    if details is not None and not isinstance(details, dict):
        return False
    exit_status = error.get("exit_status")
    return exit_status is None or (
        not isinstance(exit_status, bool)
        and isinstance(exit_status, int)
        and 1 <= exit_status <= 255
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
