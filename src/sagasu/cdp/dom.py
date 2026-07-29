"""Capture the live HTML DOM of the active Chromium page target."""

from __future__ import annotations

from dataclasses import dataclass
from typing import BinaryIO, Mapping

from sagasu.artifacts.html import MAX_HTML_BYTES
from sagasu.cdp.client import (
    DEFAULT_TIMEOUT_SECONDS,
    CDPClient,
    Connector,
    PageTarget,
    TargetLoader,
)
from sagasu.cdp.targets import select_active_page
from sagasu.protocol import SagasuError


# Kept as the DOM capture limit and a convenient test/configuration seam.
MAX_DOM_BYTES = MAX_HTML_BYTES


@dataclass(frozen=True)
class DOMSnapshot:
    html: str
    target_id: str
    title: str
    url: str
    byte_count: int


def stream_active_dom(
    destination: BinaryIO,
    *,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    target_loader: TargetLoader | None = None,
    connector: Connector | None = None,
    client: CDPClient | None = None,
) -> DOMSnapshot:
    """Serialize the active page's live DOM and write UTF-8 HTML."""

    snapshot = capture_active_dom(
        timeout=timeout,
        target_loader=target_loader,
        connector=connector,
        client=client,
    )
    encoded = snapshot.html.encode("utf-8")
    if not encoded:
        raise SagasuError(
            "dom_failed",
            "The active page returned an empty DOM",
        )
    if len(encoded) > MAX_DOM_BYTES:
        raise SagasuError(
            "dom_too_large",
            "The active page DOM exceeds the supported size limit",
            {"bytes": len(encoded), "max_bytes": MAX_DOM_BYTES},
        )
    written = destination.write(encoded)
    if written is not None and written != len(encoded):
        raise SagasuError(
            "dom_failed",
            "The active page DOM could not be streamed completely",
            {"bytes_written": written, "expected_bytes": len(encoded)},
        )
    destination.flush()
    return DOMSnapshot(
        html=snapshot.html,
        target_id=snapshot.target_id,
        title=snapshot.title,
        url=snapshot.url,
        byte_count=len(encoded),
    )


def capture_active_dom(
    *,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    target_loader: TargetLoader | None = None,
    connector: Connector | None = None,
    client: CDPClient | None = None,
) -> DOMSnapshot:
    """Return a current DOM snapshot from exactly one visible page target."""

    if client is not None and (target_loader is not None or connector is not None):
        raise SagasuError(
            "invalid_arguments",
            "Pass either a CDP client or transport overrides, not both",
            exit_status=2,
        )
    active_client = client or CDPClient(
        timeout=timeout,
        target_loader=target_loader,
        connector=connector,
    )
    targets = active_client.page_targets()
    selected = select_active_page(targets, client=active_client)
    html = _outer_html(selected, client=active_client)
    return DOMSnapshot(
        html=html,
        target_id=selected.target_id,
        title=selected.title,
        url=selected.url,
        byte_count=len(html.encode("utf-8")),
    )

def _outer_html(target: PageTarget, *, client: CDPClient) -> str:
    with client.open(target) as session:
        document = session.call(
            "DOM.getDocument",
            {"depth": 0, "pierce": False},
        )
        root = document.get("root")
        node_id = root.get("nodeId") if isinstance(root, Mapping) else None
        if isinstance(node_id, bool) or not isinstance(node_id, int):
            raise SagasuError(
                "invalid_response",
                "CDP omitted the DOM document node",
            )
        serialized = session.call(
            "DOM.getOuterHTML",
            {"nodeId": node_id},
        )
    html = serialized.get("outerHTML")
    if not isinstance(html, str) or not html:
        raise SagasuError(
            "invalid_response",
            "CDP returned an invalid serialized DOM",
        )
    return html
