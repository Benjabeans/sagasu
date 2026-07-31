from __future__ import annotations

import io
import json

import pytest

from sagasu.cdp import dom
from sagasu.protocol import SagasuError


def target(target_id: str, title: str) -> dict[str, str]:
    return {
        "id": target_id,
        "type": "page",
        "title": title,
        "url": f"https://example.test/{target_id}",
        "webSocketDebuggerUrl": (
            f"ws://127.0.0.1:9222/devtools/page/{target_id}"
        ),
    }


class FakeSocket:
    def __init__(self, *, visible: bool, html: str) -> None:
        self.visible = visible
        self.html = html
        self.response = ""
        self.closed = False

    def send(self, payload: str) -> None:
        request = json.loads(payload)
        method = request["method"]
        if method == "Runtime.evaluate":
            result = {
                "result": {
                    "type": "string",
                    "value": "visible" if self.visible else "hidden",
                }
            }
        elif method == "DOM.getDocument":
            result = {"root": {"nodeId": 7}}
        elif method == "DOM.getOuterHTML":
            result = {"outerHTML": self.html}
        else:  # pragma: no cover - the production call set is fixed
            raise AssertionError(method)
        self.response = json.dumps({"id": request["id"], "result": result})

    def recv(self) -> str:
        return self.response

    def close(self) -> None:
        self.closed = True

    def settimeout(self, timeout: float) -> None:
        assert timeout > 0


def test_streams_active_page_live_html_when_multiple_tabs_exist():
    targets = [target("hidden", "Background"), target("active", "Active")]

    def load_targets(endpoint: str, timeout: float):
        assert endpoint == "http://127.0.0.1:9222"
        assert timeout == 2
        return targets

    def connect(url: str, timeout: float):
        assert timeout == 2
        return FakeSocket(
            visible=url.endswith("/active"),
            html="<html><head><title>Live</title></head><body>now</body></html>",
        )

    output = io.BytesIO()
    snapshot = dom.stream_active_dom(
        output,
        timeout=2,
        target_loader=load_targets,
        connector=connect,
    )
    assert output.getvalue() == snapshot.html.encode()
    assert snapshot.target_id == "active"
    assert snapshot.title == "Active"
    assert snapshot.url == "https://example.test/active"
    assert snapshot.byte_count == len(output.getvalue())


def test_missing_and_ambiguous_active_pages_are_structured():
    with pytest.raises(SagasuError) as missing:
        dom.capture_active_dom(
            target_loader=lambda endpoint, timeout: [],
            connector=lambda url, timeout: pytest.fail("must not connect"),
        )
    assert missing.value.code == "dom_target_not_found"

    targets = [target("one", "One"), target("two", "Two")]
    with pytest.raises(SagasuError) as ambiguous:
        dom.capture_active_dom(
            target_loader=lambda endpoint, timeout: targets,
            connector=lambda url, timeout: FakeSocket(
                visible=True,
                html="<html></html>",
            ),
        )
    assert ambiguous.value.code == "dom_target_ambiguous"


def test_non_loopback_cdp_websocket_is_rejected():
    unsafe = target("unsafe", "Unsafe")
    unsafe["webSocketDebuggerUrl"] = "ws://example.test/devtools/page/unsafe"
    with pytest.raises(SagasuError) as error:
        dom.capture_active_dom(
            target_loader=lambda endpoint, timeout: [unsafe],
            connector=lambda url, timeout: pytest.fail("must not connect"),
        )
    assert error.value.code == "invalid_response"


def test_dom_size_limit_is_enforced_before_streaming(monkeypatch):
    monkeypatch.setattr(dom, "MAX_DOM_BYTES", 10)
    output = io.BytesIO()
    with pytest.raises(SagasuError) as error:
        dom.stream_active_dom(
            output,
            target_loader=lambda endpoint, timeout: [target("one", "One")],
            connector=lambda url, timeout: FakeSocket(
                visible=True,
                html="<html>too large</html>",
            ),
        )
    assert error.value.code == "dom_too_large"
    assert output.getvalue() == b""
