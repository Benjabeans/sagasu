"""Typed, bounded sequences of browser mutations for the session executor."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from typing import Any, Callable, Mapping, Sequence

from sagasu.cdp.insert_text import (
    TextInsertionResult,
    insert_text_active_page,
    validate_insert_text,
)
from sagasu.cdp.navigate import (
    NavigationResult,
    navigate_active_page,
    validate_navigation_url,
)
from sagasu.protocol import SagasuError, success
from sagasu.xcontrol.cursor import create_backend, normalize_button
from sagasu.xcontrol.cursor.types import CursorBackend
from sagasu.xcontrol.display import (
    DisplaySize,
    PointerPosition,
    get_pointer_position,
    validate_coordinate,
)


DEFAULT_MAX_ACTIONS = 3
DEFAULT_SETTLE_MS = 1_000
MAX_CONFIGURED_ACTIONS = 100
MAX_SETTLE_MS = 30_000
SUPPORTED_OPERATIONS = (
    "cursor.move",
    "cursor.click",
    "cursor.drag",
    "cursor.scroll",
    "text.insert",
    "page.navigate",
)


@dataclass(frozen=True)
class ActionSequenceConfig:
    max_actions: int = DEFAULT_MAX_ACTIONS
    settle_ms: int = DEFAULT_SETTLE_MS

    @classmethod
    def from_environ(
        cls, environ: Mapping[str, str]
    ) -> "ActionSequenceConfig":
        return cls(
            max_actions=_environment_integer(
                environ,
                "SAGASU_SEQUENCE_MAX_ACTIONS",
                DEFAULT_MAX_ACTIONS,
                minimum=1,
                maximum=MAX_CONFIGURED_ACTIONS,
            ),
            settle_ms=_environment_integer(
                environ,
                "SAGASU_SEQUENCE_SETTLE_MS",
                DEFAULT_SETTLE_MS,
                minimum=0,
                maximum=MAX_SETTLE_MS,
            ),
        )

    def effective_settle_ms(self, override: int | None) -> int:
        if override is None:
            return self.settle_ms
        return validate_settle_ms(override)


@dataclass(frozen=True)
class CursorMove:
    operation: str
    x: int
    y: int
    duration_ms: int | None = None
    steady: bool = False
    backend: str = "humancursor"


@dataclass(frozen=True)
class CursorClick:
    operation: str
    x: int
    y: int
    button: str = "left"
    count: int = 1
    hold_ms: int = 0
    backend: str = "humancursor"


@dataclass(frozen=True)
class CursorDrag:
    operation: str
    x1: int
    y1: int
    x2: int
    y2: int
    duration_ms: int | None = None
    steady: bool = False
    backend: str = "humancursor"


@dataclass(frozen=True)
class CursorScroll:
    operation: str
    x: int
    y: int
    steps: int
    backend: str = "humancursor"


@dataclass(frozen=True)
class TextInsert:
    operation: str
    text: str


@dataclass(frozen=True)
class PageNavigate:
    operation: str
    url: str


SequenceAction = (
    CursorMove
    | CursorClick
    | CursorDrag
    | CursorScroll
    | TextInsert
    | PageNavigate
)


@dataclass(frozen=True)
class SequenceExecution:
    results: tuple[dict[str, Any], ...]
    failure: SagasuError | None = None
    failed_index: int | None = None

    @property
    def completed(self) -> bool:
        return self.failure is None


def parse_action_sequence(
    document: str,
    *,
    max_actions: int = MAX_CONFIGURED_ACTIONS,
) -> tuple[SequenceAction, ...]:
    """Parse and fully validate one JSON action array before it can run."""

    try:
        value = json.loads(document)
    except json.JSONDecodeError as exc:
        raise SagasuError(
            "invalid_arguments",
            "--actions-json must contain valid JSON",
            {"line": exc.lineno, "column": exc.colno, "reason": exc.msg},
            exit_status=2,
        ) from exc
    if not isinstance(value, list):
        raise SagasuError(
            "invalid_arguments",
            "--actions-json must be a JSON array",
            exit_status=2,
        )
    if not value:
        raise SagasuError(
            "invalid_arguments",
            "An action sequence must contain at least one action",
            exit_status=2,
        )
    if len(value) > max_actions:
        raise SagasuError(
            "sequence_too_long",
            f"The action sequence exceeds the limit of {max_actions}",
            {"actions": len(value), "max_actions": max_actions},
            exit_status=2,
        )

    actions = tuple(_parse_action(item, index) for index, item in enumerate(value))
    for index, action in enumerate(actions[:-1]):
        if isinstance(action, PageNavigate):
            raise SagasuError(
                "invalid_arguments",
                "page.navigate may only be the final action in a sequence",
                {"index": index},
                exit_status=2,
            )
    return actions


def encode_action_sequence(actions: Sequence[SequenceAction]) -> str:
    """Serialize validated actions without relying on caller JSON formatting."""

    return json.dumps(
        [
            {
                name: value
                for name, value in asdict(action).items()
                if value is not None
            }
            for action in actions
        ],
        ensure_ascii=False,
        separators=(",", ":"),
    )


def validate_settle_ms(value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise SagasuError(
            "invalid_arguments",
            "--settle-ms must be an integer",
            exit_status=2,
        )
    if value < 0 or value > MAX_SETTLE_MS:
        raise SagasuError(
            "invalid_arguments",
            f"--settle-ms must be between 0 and {MAX_SETTLE_MS}",
            {"settle_ms": value, "maximum": MAX_SETTLE_MS},
            exit_status=2,
        )
    return value


def validate_sequence_coordinates(
    actions: Sequence[SequenceAction], display: DisplaySize
) -> None:
    """Reject every invalid coordinate before the first action is applied."""

    for index, action in enumerate(actions):
        if isinstance(action, (CursorMove, CursorClick, CursorScroll)):
            validate_coordinate(
                action.x,
                action.y,
                display,
                name=f"action {index} coordinate",
            )
        elif isinstance(action, CursorDrag):
            validate_coordinate(
                action.x1,
                action.y1,
                display,
                name=f"action {index} start coordinate",
            )
            validate_coordinate(
                action.x2,
                action.y2,
                display,
                name=f"action {index} end coordinate",
            )


def run_action_sequence(
    actions: Sequence[SequenceAction],
    display: DisplaySize,
    *,
    backend_factory: Callable[[str], CursorBackend] = create_backend,
    pointer_position: Callable[[], PointerPosition] = get_pointer_position,
    insert_text: Callable[[str], TextInsertionResult] = insert_text_active_page,
    navigate: Callable[[str], NavigationResult] = navigate_active_page,
) -> SequenceExecution:
    """Apply validated actions in order, stopping at the first expected failure."""

    backends: dict[str, CursorBackend] = {}
    results: list[dict[str, Any]] = []
    for index, action in enumerate(actions):
        try:
            result = _run_action(
                action,
                display,
                backends=backends,
                backend_factory=backend_factory,
                pointer_position=pointer_position,
                insert_text=insert_text,
                navigate=navigate,
            )
        except SagasuError as error:
            return SequenceExecution(tuple(results), error, index)
        result["index"] = index
        results.append(result)
    return SequenceExecution(tuple(results))


def _parse_action(value: object, index: int) -> SequenceAction:
    if not isinstance(value, dict):
        raise _action_error(index, "must be a JSON object")
    operation = value.get("operation")
    if not isinstance(operation, str):
        raise _action_error(index, "requires a string operation")

    if operation == "cursor.move":
        _check_fields(
            value,
            index,
            required=("operation", "x", "y"),
            optional=("duration_ms", "steady", "backend"),
        )
        return CursorMove(
            operation=operation,
            x=_integer(value, "x", index),
            y=_integer(value, "y", index),
            duration_ms=_optional_nonnegative_integer(
                value, "duration_ms", index
            ),
            steady=_optional_boolean(value, "steady", index, False),
            backend=_backend(value, index),
        )
    if operation == "cursor.click":
        _check_fields(
            value,
            index,
            required=("operation", "x", "y"),
            optional=("button", "count", "hold_ms", "backend"),
        )
        button = _optional_string(value, "button", index, "left")
        button = normalize_button(button)[0]
        count = _optional_integer(value, "count", index, 1)
        if count <= 0:
            raise _action_error(index, "count must be greater than zero")
        return CursorClick(
            operation=operation,
            x=_integer(value, "x", index),
            y=_integer(value, "y", index),
            button=button,
            count=count,
            hold_ms=_optional_nonnegative_integer(
                value, "hold_ms", index, default=0
            ),
            backend=_backend(value, index),
        )
    if operation == "cursor.drag":
        _check_fields(
            value,
            index,
            required=("operation", "x1", "y1", "x2", "y2"),
            optional=("duration_ms", "steady", "backend"),
        )
        return CursorDrag(
            operation=operation,
            x1=_integer(value, "x1", index),
            y1=_integer(value, "y1", index),
            x2=_integer(value, "x2", index),
            y2=_integer(value, "y2", index),
            duration_ms=_optional_nonnegative_integer(
                value, "duration_ms", index
            ),
            steady=_optional_boolean(value, "steady", index, False),
            backend=_backend(value, index),
        )
    if operation == "cursor.scroll":
        _check_fields(
            value,
            index,
            required=("operation", "x", "y", "steps"),
            optional=("backend",),
        )
        steps = _integer(value, "steps", index)
        if steps == 0:
            raise _action_error(index, "steps cannot be zero")
        return CursorScroll(
            operation=operation,
            x=_integer(value, "x", index),
            y=_integer(value, "y", index),
            steps=steps,
            backend=_backend(value, index),
        )
    if operation == "text.insert":
        _check_fields(
            value,
            index,
            required=("operation", "text"),
            optional=(),
        )
        text = _string(value, "text", index)
        validate_insert_text(text)
        return TextInsert(operation=operation, text=text)
    if operation == "page.navigate":
        _check_fields(
            value,
            index,
            required=("operation", "url"),
            optional=(),
        )
        url = _string(value, "url", index)
        validate_navigation_url(url)
        return PageNavigate(operation=operation, url=url)
    raise _action_error(
        index,
        "has an unsupported operation",
        {"operation": operation, "supported": list(SUPPORTED_OPERATIONS)},
    )


def _run_action(
    action: SequenceAction,
    display: DisplaySize,
    *,
    backends: dict[str, CursorBackend],
    backend_factory: Callable[[str], CursorBackend],
    pointer_position: Callable[[], PointerPosition],
    insert_text: Callable[[str], TextInsertionResult],
    navigate: Callable[[str], NavigationResult],
) -> dict[str, Any]:
    if isinstance(action, TextInsert):
        inserted = insert_text(action.text)
        return _success(
            action.operation,
            "cdp",
            display,
            pointer_position(),
            target_id=inserted.target_id,
            title=inserted.title,
            url=inserted.url,
            characters=inserted.character_count,
            bytes=inserted.byte_count,
        )
    if isinstance(action, PageNavigate):
        navigated = navigate(action.url)
        return _success(
            action.operation,
            "cdp",
            display,
            pointer_position(),
            target_id=navigated.target_id,
            requested_url=navigated.requested_url,
            frame_id=navigated.frame_id,
            loader_id=navigated.loader_id,
            is_download=navigated.is_download,
        )

    backend = backends.get(action.backend)
    if backend is None:
        backend = backend_factory(action.backend)
        backends[action.backend] = backend
    if isinstance(action, CursorMove):
        backend.move(
            action.x,
            action.y,
            duration=_seconds(action.duration_ms),
            steady=action.steady,
        )
    elif isinstance(action, CursorClick):
        backend.click(
            action.x,
            action.y,
            button=action.button,
            count=action.count,
            hold=action.hold_ms / 1000,
        )
    elif isinstance(action, CursorDrag):
        backend.drag(
            action.x1,
            action.y1,
            action.x2,
            action.y2,
            duration=_seconds(action.duration_ms),
            steady=action.steady,
        )
    else:
        assert isinstance(action, CursorScroll)
        backend.scroll(action.x, action.y, steps=action.steps)
    return _success(
        action.operation,
        action.backend,
        display,
        pointer_position(),
    )


def _success(
    operation: str,
    backend: str,
    display: DisplaySize,
    pointer: PointerPosition,
    **extra: object,
) -> dict[str, Any]:
    return success(
        operation,
        backend=backend,
        width=display.width,
        height=display.height,
        pointer_x=pointer.x,
        pointer_y=pointer.y,
        **extra,
    )


def _check_fields(
    value: Mapping[str, object],
    index: int,
    *,
    required: Sequence[str],
    optional: Sequence[str],
) -> None:
    missing = sorted(set(required) - value.keys())
    unknown = sorted(value.keys() - set(required) - set(optional))
    if missing:
        raise _action_error(index, "is missing required fields", {"missing": missing})
    if unknown:
        raise _action_error(index, "contains unknown fields", {"unknown": unknown})


def _integer(value: Mapping[str, object], name: str, index: int) -> int:
    item = value.get(name)
    if isinstance(item, bool) or not isinstance(item, int):
        raise _action_error(index, f"{name} must be an integer")
    return item


def _optional_integer(
    value: Mapping[str, object], name: str, index: int, default: int
) -> int:
    if name not in value:
        return default
    return _integer(value, name, index)


def _optional_nonnegative_integer(
    value: Mapping[str, object],
    name: str,
    index: int,
    *,
    default: int | None = None,
) -> int | None:
    if name not in value:
        return default
    item = _integer(value, name, index)
    if item < 0:
        raise _action_error(index, f"{name} must be zero or greater")
    return item


def _string(value: Mapping[str, object], name: str, index: int) -> str:
    item = value.get(name)
    if not isinstance(item, str):
        raise _action_error(index, f"{name} must be a string")
    return item


def _optional_string(
    value: Mapping[str, object], name: str, index: int, default: str
) -> str:
    if name not in value:
        return default
    return _string(value, name, index)


def _optional_boolean(
    value: Mapping[str, object], name: str, index: int, default: bool
) -> bool:
    if name not in value:
        return default
    item = value.get(name)
    if not isinstance(item, bool):
        raise _action_error(index, f"{name} must be a boolean")
    return item


def _backend(value: Mapping[str, object], index: int) -> str:
    backend = _optional_string(value, "backend", index, "humancursor")
    if backend not in ("humancursor", "xdotool"):
        raise _action_error(
            index,
            "backend must be humancursor or xdotool",
            {"backend": backend},
        )
    return backend


def _seconds(milliseconds: int | None) -> float | None:
    return None if milliseconds is None else milliseconds / 1000


def _environment_integer(
    environ: Mapping[str, str],
    name: str,
    default: int,
    *,
    minimum: int,
    maximum: int,
) -> int:
    raw = environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise SagasuError(
            "invalid_arguments",
            f"{name} must be an integer",
            {"value": raw},
            exit_status=2,
        ) from exc
    if value < minimum or value > maximum:
        raise SagasuError(
            "invalid_arguments",
            f"{name} must be between {minimum} and {maximum}",
            {"value": value, "minimum": minimum, "maximum": maximum},
            exit_status=2,
        )
    return value


def _action_error(
    index: int,
    message: str,
    details: Mapping[str, object] | None = None,
) -> SagasuError:
    payload = {"index": index}
    if details:
        payload.update(details)
    return SagasuError(
        "invalid_arguments",
        f"Action {index} {message}",
        payload,
        exit_status=2,
    )
