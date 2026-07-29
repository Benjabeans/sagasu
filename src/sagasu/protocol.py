"""Stable JSON protocol shared by the host CLI and container executor."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Mapping, NoReturn, TextIO


@dataclass
class SagasuError(Exception):
    """An expected failure that is safe to serialize for an agent."""

    code: str
    message: str
    details: Mapping[str, Any] = field(default_factory=dict)
    exit_status: int = 1

    def __post_init__(self) -> None:
        super().__init__(self.message)

    def as_dict(self) -> dict[str, Any]:
        error: dict[str, Any] = {
            "ok": False,
            "error": {
                "code": self.code,
                "message": self.message,
            },
        }
        if self.details:
            error["error"]["details"] = dict(self.details)
        return error

    @classmethod
    def from_payload(
        cls, payload: Mapping[str, Any], *, default_code: str = "input_failed"
    ) -> "SagasuError":
        raw_error = payload.get("error")
        if not isinstance(raw_error, Mapping):
            return cls(
                default_code,
                "The session-control executor returned an invalid error",
            )
        code = raw_error.get("code")
        message = raw_error.get("message")
        details = raw_error.get("details")
        return cls(
            str(code) if code else default_code,
            str(message) if message else "The session-control executor failed",
            details if isinstance(details, Mapping) else {},
        )


def success(
    operation: str,
    *,
    backend: str,
    width: int,
    height: int,
    pointer_x: int,
    pointer_y: int,
    **extra: Any,
) -> dict[str, Any]:
    """Build the canonical successful response from an in-container command."""

    payload: dict[str, Any] = {
        "ok": True,
        "operation": operation,
        "backend": backend,
        "display": {"width": width, "height": height},
        "pointer": {"x": pointer_x, "y": pointer_y},
    }
    payload.update(extra)
    return payload


def write_json(payload: Mapping[str, Any], stream: TextIO) -> None:
    json.dump(payload, stream, sort_keys=True, separators=(",", ":"))
    stream.write("\n")
    stream.flush()


def fail(error: SagasuError, stream: TextIO) -> NoReturn:
    write_json(error.as_dict(), stream)
    raise SystemExit(error.exit_status)


def parse_json_object(data: bytes | str, *, source: str) -> dict[str, Any]:
    try:
        text = data.decode("utf-8") if isinstance(data, bytes) else data
        value = json.loads(text)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SagasuError(
            "invalid_response",
            f"{source} returned invalid JSON",
            {"reason": str(exc)},
        ) from exc
    if not isinstance(value, dict):
        raise SagasuError(
            "invalid_response",
            f"{source} returned a JSON value that is not an object",
        )
    return value
