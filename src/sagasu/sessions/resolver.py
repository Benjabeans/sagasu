"""Resolve a UUID session label or explicit debug container."""

from __future__ import annotations

from typing import Any, Mapping
from uuid import UUID

from sagasu.protocol import SagasuError
from sagasu.sessions.docker import DockerCLI
from sagasu.sessions.models import (
    SESSION_LABEL,
    ContainerSummary,
    ResolvedSession,
    parse_labels,
)


def normalize_session_id(value: str) -> str:
    try:
        parsed = UUID(value)
    except (AttributeError, TypeError, ValueError) as exc:
        raise SagasuError(
            "invalid_session",
            "SESSION must be a UUID4",
            {"session_id": value},
            exit_status=2,
        ) from exc
    if parsed.version != 4:
        raise SagasuError(
            "invalid_session",
            "SESSION must be a UUID4",
            {"session_id": value},
            exit_status=2,
        )
    return str(parsed)


def resolve_session(
    docker: DockerCLI,
    *,
    session_id: str | None = None,
    container: str | None = None,
) -> ResolvedSession:
    if bool(session_id) == bool(container):
        raise SagasuError(
            "invalid_arguments",
            "Provide exactly one of SESSION or --container NAME",
            exit_status=2,
        )
    if container is not None:
        return _resolve_container(docker, container)
    assert session_id is not None
    canonical_id = normalize_session_id(session_id)
    candidates = docker.containers_for_session(canonical_id)
    running = [item for item in candidates if item.state.casefold() == "running"]
    if not candidates:
        raise SagasuError(
            "session_not_found",
            f"No Sagasu container has session ID {canonical_id}",
            {"session_id": canonical_id},
        )
    if not running:
        raise SagasuError(
            "session_not_running",
            f"Sagasu session {canonical_id} is not running",
            {
                "session_id": canonical_id,
                "containers": [_summary_details(item) for item in candidates],
            },
        )
    if len(running) != 1:
        raise SagasuError(
            "session_ambiguous",
            f"More than one running container has session ID {canonical_id}",
            {
                "session_id": canonical_id,
                "containers": [_summary_details(item) for item in running],
            },
        )
    selected = running[0]
    return ResolvedSession(
        session_id=canonical_id,
        container_id=selected.container_id,
        container_name=selected.name,
    )


def _resolve_container(docker: DockerCLI, container: str) -> ResolvedSession:
    if not container.strip():
        raise SagasuError(
            "invalid_arguments",
            "--container NAME cannot be empty",
            exit_status=2,
        )
    inspected = docker.inspect_container(container)
    state = inspected.get("State")
    if not isinstance(state, Mapping) or state.get("Running") is not True:
        raise SagasuError(
            "session_not_running",
            f"Container {container!r} is not running",
            {"container": container},
        )
    config = inspected.get("Config")
    labels: dict[str, str] = {}
    if isinstance(config, Mapping):
        labels = parse_labels(config.get("Labels"))
    if not any(key.startswith("computer.sagasu.") for key in labels):
        raise SagasuError(
            "not_sagasu_container",
            f"Container {container!r} is not a Sagasu session",
            {"container": container},
        )
    container_id = str(inspected.get("Id") or inspected.get("ID") or "")
    if not container_id:
        raise SagasuError(
            "invalid_response",
            "Docker inspect omitted the container ID",
            {"container": container},
        )
    inspected_name = str(inspected.get("Name") or container).lstrip("/")
    raw_session_id = labels.get(SESSION_LABEL)
    resolved_id: str | None = None
    if raw_session_id:
        try:
            resolved_id = normalize_session_id(raw_session_id)
        except SagasuError:
            # The explicit override exists partly to debug containers with
            # incomplete metadata. Preserve the label in Docker, but do not
            # claim it is a valid session ID in the response.
            resolved_id = None
    return ResolvedSession(
        session_id=resolved_id,
        container_id=container_id,
        container_name=inspected_name,
    )


def _summary_details(item: ContainerSummary) -> dict[str, Any]:
    return {
        "container_id": item.container_id,
        "name": item.name,
        "state": item.state,
    }
