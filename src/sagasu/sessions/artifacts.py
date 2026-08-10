"""Host-side composition for artifacts streamed from one resolved session."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from sagasu.artifacts.atomic import publish_reserved_stream, publish_stream
from sagasu.artifacts.html import validate_html
from sagasu.artifacts.png import validate_png
from sagasu.protocol import SagasuError
from sagasu.sessions.executor import SessionExecutor, validate_executor_result


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


def save_action_sequence_screenshot(
    executor: SessionExecutor,
    destination: Path | str,
    *,
    executor_arguments: list[str],
    executor_input: bytes,
    overwrite: bool,
) -> dict[str, Any]:
    """Apply one action sequence and atomically publish its final screenshot."""

    try:
        artifact = publish_reserved_stream(
            destination,
            overwrite=overwrite,
            artifact_name="sequence screenshot",
            stream_writer=lambda stream: executor.stream_json(
                executor_arguments,
                stream,
                input_data=executor_input,
                failure_code="sequence_failed",
                failure_message="The in-container action sequence failed",
            ),
            validator=_validate_action_sequence_artifact,
        )
    except SagasuError as error:
        if "sequence_state" not in error.details:
            raise
        _raise_validated_sequence_observation_failure(executor, error)
    payload = artifact.stream_result
    payload["output"] = str(artifact.path)
    if payload["completed"] is not True:
        _raise_action_sequence_failure(payload, artifact.path)
    return payload


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


def _validate_action_sequence_artifact(
    path: Path, payload: dict[str, Any]
) -> tuple[int, int]:
    width, height = validate_png(path)
    if payload.get("operation") != "actions.sequence":
        raise SagasuError(
            "invalid_response",
            "The session executor returned the wrong sequence operation",
        )
    _validate_action_sequence_state(payload)

    display = payload.get("display")
    assert isinstance(display, dict)
    if (display.get("width"), display.get("height")) != (width, height):
        raise SagasuError(
            "invalid_response",
            "The sequence screenshot dimensions do not match its metadata",
        )
    return width, height


def _validate_action_sequence_state(payload: dict[str, Any]) -> None:
    """Validate mutation status independently of the final screenshot."""

    completed = payload.get("completed")
    if not isinstance(completed, bool):
        raise SagasuError(
            "invalid_response",
            "The session executor returned an invalid sequence status",
        )
    action_count = _payload_integer(payload, "action_count", minimum=1)
    actions_completed = _payload_integer(
        payload, "actions_completed", minimum=0
    )
    _payload_integer(payload, "settle_ms", minimum=0)
    results = payload.get("results")
    if not isinstance(results, list) or len(results) != actions_completed:
        raise SagasuError(
            "invalid_response",
            "The session executor returned invalid sequence results",
        )
    for index, result in enumerate(results):
        if not isinstance(result, dict):
            raise SagasuError(
                "invalid_response",
                "The session executor returned an invalid action result",
            )
        validate_executor_result(
            result, allow_pointer_observation_failure=True
        )
        if (
            result.get("ok") is not True
            or result.get("index") != index
            or not isinstance(result.get("operation"), str)
            or not isinstance(result.get("backend"), str)
            or result.get("display") != payload.get("display")
            or "text" in result
        ):
            raise SagasuError(
                "invalid_response",
                "The session executor returned an inconsistent action result",
            )
    if actions_completed > action_count:
        raise SagasuError(
            "invalid_response",
            "The session executor completed too many sequence actions",
        )
    if not isinstance(payload.get("pointer_included"), bool):
        raise SagasuError(
            "invalid_response",
            "The session executor returned invalid screenshot metadata",
        )

    display = payload.get("display")
    if not isinstance(display, dict) or not all(
        not isinstance(display.get(name), bool)
        and isinstance(display.get(name), int)
        and display[name] > 0
        for name in ("width", "height")
    ):
        raise SagasuError(
            "invalid_response",
            "The session executor returned invalid sequence display dimensions",
        )
    if completed:
        if actions_completed != action_count or any(
            key in payload for key in ("failed_index", "failure")
        ):
            raise SagasuError(
                "invalid_response",
                "The completed sequence returned failure metadata",
            )
    else:
        failed_index = _payload_integer(payload, "failed_index", minimum=0)
        failure = payload.get("failure")
        if (
            failed_index != actions_completed
            or failed_index >= action_count
            or not isinstance(failure, dict)
        ):
            raise SagasuError(
                "invalid_response",
                "The failed sequence returned incomplete failure metadata",
            )
        if not all(
            isinstance(failure.get(key), str) and failure.get(key)
            for key in ("code", "message")
        ):
            raise SagasuError(
                "invalid_response",
                "The failed sequence returned an invalid error",
            )


def _raise_validated_sequence_observation_failure(
    executor: SessionExecutor,
    error: SagasuError,
) -> None:
    """Validate container-authored mutation state before surfacing it."""

    state = error.details.get("sequence_state")
    if not isinstance(state, dict):
        raise SagasuError(
            "invalid_response",
            "The session executor returned invalid sequence failure metadata",
        ) from error
    _validate_action_sequence_state(state)

    required_observation = {
        "observation_stage",
        "settle_completed",
        "screenshot_captured",
        "pointer_observed",
    }
    if not required_observation <= state.keys():
        raise SagasuError(
            "invalid_response",
            "The session executor returned incomplete observation failure metadata",
        ) from error
    stage = state.get("observation_stage")
    screenshot_captured = state.get("screenshot_captured")
    valid_stage = stage in ("screenshot", "pointer")
    if (
        not valid_stage
        or state.get("settle_completed") is not True
        or state.get("pointer_observed") is not False
        or not isinstance(screenshot_captured, bool)
        or screenshot_captured != (stage == "pointer")
    ):
        raise SagasuError(
            "invalid_response",
            "The session executor returned inconsistent observation failure metadata",
        ) from error

    details = dict(error.details)
    details["session_id"] = executor.session.session_id
    details["container_id"] = executor.session.container_id
    raise SagasuError(
        error.code,
        error.message,
        details,
        exit_status=error.exit_status,
    ) from error


def _payload_integer(
    payload: dict[str, Any], name: str, *, minimum: int
) -> int:
    value = payload.get(name)
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise SagasuError(
            "invalid_response",
            f"The session executor returned an invalid {name}",
        )
    return value


def _raise_action_sequence_failure(payload: dict[str, Any], path: Path) -> None:
    error = SagasuError.from_payload(
        {"error": payload["failure"]},
        default_code="sequence_failed",
    )
    details = dict(error.details)
    details.update(
        {
            "output": str(path),
            "failed_index": payload["failed_index"],
            "actions_completed": payload["actions_completed"],
            "action_count": payload["action_count"],
        }
    )
    raise SagasuError(
        error.code,
        error.message,
        details,
        exit_status=error.exit_status,
    )
