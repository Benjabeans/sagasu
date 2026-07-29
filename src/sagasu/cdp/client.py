"""Small, loopback-only Chrome DevTools Protocol transport."""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from types import TracebackType
from typing import Any, Callable, Mapping, Protocol
from urllib.parse import urlsplit

from sagasu.protocol import SagasuError


DEFAULT_CDP_PORT = 9222
DEFAULT_TIMEOUT_SECONDS = 5.0
MAX_TARGET_LIST_BYTES = 2 * 1024 * 1024


class WebSocketConnection(Protocol):
    def send(self, payload: str) -> object: ...

    def recv(self) -> str | bytes: ...

    def close(self) -> object: ...

    def settimeout(self, timeout: float) -> object: ...


TargetLoader = Callable[[str, float], object]
Connector = Callable[[str, float], WebSocketConnection]


@dataclass(frozen=True)
class PageTarget:
    target_id: str
    title: str
    url: str
    websocket_url: str


class CDPClient:
    """Discover browser pages and open request sessions to them."""

    def __init__(
        self,
        *,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        endpoint: str | None = None,
        target_loader: TargetLoader | None = None,
        connector: Connector | None = None,
    ) -> None:
        if timeout <= 0:
            raise SagasuError(
                "invalid_arguments",
                "The CDP timeout must be greater than zero",
                {"timeout": timeout},
                exit_status=2,
            )
        self.timeout = timeout
        self.endpoint = endpoint or cdp_endpoint()
        self._target_loader = target_loader or load_targets
        self._connector = connector or connect_websocket

    def page_targets(self) -> list[PageTarget]:
        payload = self._target_loader(self.endpoint, self.timeout)
        return parse_page_targets(payload)

    def open(self, target: PageTarget) -> "CDPSession":
        socket = self._connector(target.websocket_url, self.timeout)
        return CDPSession(socket, timeout=self.timeout)


class CDPSession:
    """A numbered sequence of CDP calls over one page WebSocket."""

    def __init__(
        self,
        socket: WebSocketConnection,
        *,
        timeout: float,
    ) -> None:
        self._socket = socket
        self._timeout = timeout
        self._next_call_id = 1

    def __enter__(self) -> "CDPSession":
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        del exc_type, exc_value, traceback
        self.close()

    def call(
        self,
        method: str,
        params: Mapping[str, Any] | None = None,
    ) -> Mapping[str, Any]:
        call_id = self._next_call_id
        self._next_call_id += 1
        return _call(
            self._socket,
            call_id=call_id,
            method=method,
            params=params or {},
            timeout=self._timeout,
        )

    def close(self) -> None:
        try:
            self._socket.close()
        except Exception:
            # A received result remains valid even if the close frame fails.
            pass


def cdp_endpoint() -> str:
    """Return the configured loopback CDP HTTP endpoint."""

    raw_port = os.environ.get("CDP_PORT", str(DEFAULT_CDP_PORT))
    try:
        port = int(raw_port)
    except ValueError as exc:
        raise SagasuError(
            "dom_unavailable",
            "CDP_PORT is not a valid TCP port",
            {"port": raw_port},
        ) from exc
    if port < 1 or port > 65535:
        raise SagasuError(
            "dom_unavailable",
            "CDP_PORT is outside the valid TCP port range",
            {"port": port},
        )
    return f"http://127.0.0.1:{port}"


def load_targets(endpoint: str, timeout: float) -> object:
    """Load the browser's target list without honoring proxy variables."""

    request = urllib.request.Request(
        f"{endpoint}/json/list",
        headers={"Accept": "application/json"},
        method="GET",
    )
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(request, timeout=timeout) as response:
            payload = response.read(MAX_TARGET_LIST_BYTES + 1)
    except (OSError, urllib.error.URLError) as exc:
        raise SagasuError(
            "dom_unavailable",
            "The browser CDP endpoint is unavailable",
            {"reason": str(exc)},
        ) from exc
    if len(payload) > MAX_TARGET_LIST_BYTES:
        raise SagasuError(
            "invalid_response",
            "The browser returned an unreasonably large CDP target list",
        )
    try:
        return json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SagasuError(
            "invalid_response",
            "The browser returned an invalid CDP target list",
            {"reason": str(exc)},
        ) from exc


def parse_page_targets(payload: object) -> list[PageTarget]:
    """Validate and normalize inspectable page targets."""

    if not isinstance(payload, list):
        raise SagasuError(
            "invalid_response",
            "The browser CDP target list is not an array",
        )
    targets: list[PageTarget] = []
    for item in payload:
        if not isinstance(item, Mapping) or item.get("type") != "page":
            continue
        target_id = item.get("id")
        title = item.get("title")
        url = item.get("url")
        websocket_url = item.get("webSocketDebuggerUrl")
        if not all(
            isinstance(value, str) and value
            for value in (target_id, websocket_url)
        ):
            continue
        assert isinstance(target_id, str)
        assert isinstance(websocket_url, str)
        require_loopback_websocket(websocket_url)
        targets.append(
            PageTarget(
                target_id=target_id,
                title=title if isinstance(title, str) else "",
                url=url if isinstance(url, str) else "",
                websocket_url=websocket_url,
            )
        )
    if not targets:
        raise SagasuError(
            "dom_target_not_found",
            "The session has no inspectable browser page",
        )
    return targets


def require_loopback_websocket(url: str) -> None:
    """Reject browser-supplied CDP sockets outside the session container."""

    parsed = urlsplit(url)
    if parsed.scheme != "ws" or parsed.hostname not in {
        "127.0.0.1",
        "localhost",
        "::1",
    }:
        raise SagasuError(
            "invalid_response",
            "The browser returned a non-loopback CDP WebSocket URL",
        )


def connect_websocket(url: str, timeout: float) -> WebSocketConnection:
    """Open a browser-page CDP WebSocket."""

    try:
        import websocket
    except ImportError as exc:
        raise SagasuError(
            "dom_unavailable",
            "The CDP WebSocket client is not installed in the session",
        ) from exc
    try:
        return websocket.create_connection(
            url,
            timeout=timeout,
            suppress_origin=True,
        )
    except Exception as exc:
        raise SagasuError(
            "dom_unavailable",
            "The browser page CDP socket is unavailable",
            {"reason": str(exc)},
        ) from exc


def _call(
    socket: WebSocketConnection,
    *,
    call_id: int,
    method: str,
    params: Mapping[str, Any],
    timeout: float,
) -> Mapping[str, Any]:
    request = {
        "id": call_id,
        "method": method,
        "params": dict(params),
    }
    try:
        socket.send(json.dumps(request, separators=(",", ":")))
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(f"{method} timed out")
            socket.settimeout(remaining)
            message = socket.recv()
            if isinstance(message, bytes):
                message = message.decode("utf-8")
            response = json.loads(message)
            if not isinstance(response, Mapping) or response.get("id") != call_id:
                continue
            error = response.get("error")
            if isinstance(error, Mapping):
                reason = error.get("message")
                raise SagasuError(
                    "dom_failed",
                    f"CDP {method} failed",
                    {"reason": str(reason) if reason else "unknown CDP error"},
                )
            result = response.get("result")
            if not isinstance(result, Mapping):
                raise SagasuError(
                    "invalid_response",
                    f"CDP {method} returned an invalid result",
                )
            return result
    except SagasuError:
        raise
    except Exception as exc:
        raise SagasuError(
            "dom_failed",
            f"CDP {method} did not complete",
            {"reason": str(exc)},
        ) from exc

