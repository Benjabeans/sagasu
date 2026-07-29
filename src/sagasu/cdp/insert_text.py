"""Insert text into the focused page element through CDP."""

from __future__ import annotations

from dataclasses import dataclass

from sagasu.cdp.client import (
    DEFAULT_TIMEOUT_SECONDS,
    CDPClient,
    Connector,
    TargetLoader,
)
from sagasu.cdp.targets import select_active_page
from sagasu.protocol import SagasuError


MAX_INSERT_TEXT_BYTES = 64 * 1024


@dataclass(frozen=True)
class TextInsertionResult:
    target_id: str
    title: str
    url: str
    character_count: int
    byte_count: int


def validate_insert_text(text: str) -> None:
    """Reject empty or unreasonably large text before contacting the browser."""

    encoded = text.encode("utf-8")
    if not encoded:
        raise SagasuError(
            "invalid_arguments",
            "Text insertion requires non-empty text",
            exit_status=2,
        )
    if len(encoded) > MAX_INSERT_TEXT_BYTES:
        raise SagasuError(
            "invalid_arguments",
            "The text is too large to insert in one operation",
            {"bytes": len(encoded), "max_bytes": MAX_INSERT_TEXT_BYTES},
            exit_status=2,
        )


def insert_text_active_page(
    text: str,
    *,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    target_loader: TargetLoader | None = None,
    connector: Connector | None = None,
    client: CDPClient | None = None,
) -> TextInsertionResult:
    """Insert text into the element already focused through the X display."""

    validate_insert_text(text)
    active_client = _client(
        timeout=timeout,
        target_loader=target_loader,
        connector=connector,
        client=client,
    )
    try:
        target = select_active_page(
            active_client.page_targets(),
            client=active_client,
        )
        with active_client.open(target) as session:
            session.call("Input.insertText", {"text": text})
    except SagasuError as error:
        _translate_transport_error(error)

    encoded = text.encode("utf-8")
    return TextInsertionResult(
        target_id=target.target_id,
        title=target.title,
        url=target.url,
        character_count=len(text),
        byte_count=len(encoded),
    )


def _client(
    *,
    timeout: float,
    target_loader: TargetLoader | None,
    connector: Connector | None,
    client: CDPClient | None,
) -> CDPClient:
    if client is not None and (target_loader is not None or connector is not None):
        raise SagasuError(
            "invalid_arguments",
            "Pass either a CDP client or transport overrides, not both",
            exit_status=2,
        )
    return client or CDPClient(
        timeout=timeout,
        target_loader=target_loader,
        connector=connector,
    )


def _translate_transport_error(error: SagasuError) -> None:
    if error.code not in ("dom_failed", "dom_unavailable"):
        raise error
    raise SagasuError(
        "text_input_failed",
        "CDP could not insert text into the active page",
        error.details,
    ) from error
