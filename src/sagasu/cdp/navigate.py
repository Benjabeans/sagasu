"""Navigate the visible browser page through CDP."""

from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlsplit

from sagasu.cdp.client import (
    DEFAULT_TIMEOUT_SECONDS,
    CDPClient,
    Connector,
    TargetLoader,
)
from sagasu.cdp.targets import select_active_page
from sagasu.protocol import SagasuError


MAX_NAVIGATION_URL_BYTES = 16 * 1024


@dataclass(frozen=True)
class NavigationResult:
    target_id: str
    requested_url: str
    frame_id: str
    loader_id: str | None
    is_download: bool


def validate_navigation_url(url: str) -> None:
    """Require a bounded absolute web URL without silently rewriting it."""

    encoded = url.encode("utf-8")
    if not encoded:
        raise SagasuError(
            "invalid_arguments",
            "Navigation requires a URL",
            exit_status=2,
        )
    if len(encoded) > MAX_NAVIGATION_URL_BYTES:
        raise SagasuError(
            "invalid_arguments",
            "The navigation URL is too long",
            {"bytes": len(encoded), "max_bytes": MAX_NAVIGATION_URL_BYTES},
            exit_status=2,
        )
    if any(ord(character) < 0x20 or ord(character) == 0x7F for character in url):
        raise SagasuError(
            "invalid_arguments",
            "The navigation URL contains control characters",
            exit_status=2,
        )
    try:
        parsed = urlsplit(url)
        hostname = parsed.hostname
    except ValueError as exc:
        raise SagasuError(
            "invalid_arguments",
            "The navigation URL is invalid",
            {"reason": str(exc)},
            exit_status=2,
        ) from exc
    if parsed.scheme not in ("http", "https") or not hostname:
        raise SagasuError(
            "invalid_arguments",
            "Navigation requires an absolute HTTP or HTTPS URL",
            {"scheme": parsed.scheme or None},
            exit_status=2,
        )


def navigate_active_page(
    url: str,
    *,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    target_loader: TargetLoader | None = None,
    connector: Connector | None = None,
    client: CDPClient | None = None,
) -> NavigationResult:
    """Navigate the visible page target to an absolute HTTP(S) URL."""

    validate_navigation_url(url)
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
            result = session.call("Page.navigate", {"url": url})
    except SagasuError as error:
        _translate_transport_error(error)

    error_text = result.get("errorText")
    if isinstance(error_text, str) and error_text:
        raise SagasuError(
            "navigation_failed",
            "The browser could not navigate to the requested URL",
            {"reason": error_text, "url": url},
        )
    frame_id = result.get("frameId")
    if not isinstance(frame_id, str) or not frame_id:
        raise SagasuError(
            "invalid_response",
            "CDP omitted the navigation frame ID",
        )
    loader_id = result.get("loaderId")
    if loader_id is not None and not isinstance(loader_id, str):
        raise SagasuError(
            "invalid_response",
            "CDP returned an invalid navigation loader ID",
        )
    is_download = result.get("isDownload", False)
    if not isinstance(is_download, bool):
        raise SagasuError(
            "invalid_response",
            "CDP returned an invalid navigation download flag",
        )
    return NavigationResult(
        target_id=target.target_id,
        requested_url=url,
        frame_id=frame_id,
        loader_id=loader_id,
        is_download=is_download,
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
        "navigation_failed",
        "CDP could not navigate the active page",
        error.details,
    ) from error
