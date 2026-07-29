"""Host-side composition for artifacts streamed from one resolved session."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from sagasu.artifacts.atomic import publish_stream
from sagasu.artifacts.html import validate_html
from sagasu.artifacts.png import validate_png
from sagasu.protocol import SagasuError
from sagasu.sessions.executor import SessionExecutor


def save_screenshot(
    executor: SessionExecutor,
    destination: Path | str,
    *,
    include_pointer: bool,
    overwrite: bool,
) -> dict[str, Any]:
    """Stream, validate, and atomically save a session screenshot."""

    arguments = ["screenshot"]
    if not include_pointer:
        arguments.append("--no-pointer")
    artifact = publish_stream(
        destination,
        overwrite=overwrite,
        artifact_name="screenshot",
        stream_writer=lambda stream: executor.stream(arguments, stream),
        validator=lambda path, stream_result: validate_png(path),
    )
    width, height = artifact.validation
    return {
        "ok": True,
        "operation": "screenshot",
        "session_id": executor.session.session_id,
        "container_id": executor.session.container_id,
        "output": str(artifact.path),
        "pointer_included": include_pointer,
        "display": {"width": width, "height": height},
    }


def save_dom(
    executor: SessionExecutor,
    destination: Path | str,
    *,
    overwrite: bool,
) -> dict[str, Any]:
    """Stream, validate, and atomically save the active page's live DOM."""

    artifact = publish_stream(
        destination,
        overwrite=overwrite,
        artifact_name="DOM",
        stream_writer=lambda stream: executor.stream_json(["dom"], stream),
        validator=_validate_dom_artifact,
    )
    payload = artifact.stream_result
    payload["output"] = str(artifact.path)
    return payload


def _validate_dom_artifact(path: Path, payload: dict[str, Any]) -> int:
    """Validate both the streamed document and its executor metadata."""

    byte_count = validate_html(path)
    if payload.get("operation") != "dom.fetch":
        raise SagasuError(
            "invalid_response",
            "The session executor returned the wrong DOM operation",
        )
    reported_bytes = payload.get("bytes")
    if (
        isinstance(reported_bytes, bool)
        or not isinstance(reported_bytes, int)
        or reported_bytes != byte_count
    ):
        raise SagasuError(
            "invalid_response",
            "The session executor returned an invalid DOM byte count",
        )
    if not all(
        isinstance(payload.get(key), str)
        for key in ("target_id", "title", "url")
    ):
        raise SagasuError(
            "invalid_response",
            "The session executor returned incomplete DOM metadata",
        )
    return byte_count
