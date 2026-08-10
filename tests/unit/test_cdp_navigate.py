from __future__ import annotations

import pytest

from sagasu.cdp import navigate
from sagasu.cdp.client import PageTarget
from sagasu.protocol import SagasuError


TARGET = PageTarget(
    target_id="target-1",
    title="Example",
    url="https://example.test/current",
    websocket_url="ws://127.0.0.1:9222/devtools/page/target-1",
)


class FakeSession:
    def __init__(self, response):
        self.response = response
        self.calls = []
        self.closed = False

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.closed = True

    def call(self, method, params=None):
        self.calls.append((method, params or {}))
        if isinstance(self.response, Exception):
            raise self.response
        return self.response


class FakeClient:
    def __init__(self, response):
        self.session = FakeSession(response)
        self.opened = []

    def page_targets(self):
        return [TARGET]

    def open(self, target):
        self.opened.append(target)
        return self.session


def test_navigate_active_page_uses_page_navigate():
    client = FakeClient(
        {
            "frameId": "frame-1",
            "loaderId": "loader-1",
        }
    )
    result = navigate.navigate_active_page(
        "https://example.test/next?q=one",
        client=client,
    )
    assert client.opened == [TARGET]
    assert client.session.calls == [
        (
            "Page.navigate",
            {"url": "https://example.test/next?q=one"},
        )
    ]
    assert client.session.closed is True
    assert result == navigate.NavigationResult(
        target_id="target-1",
        requested_url="https://example.test/next?q=one",
        frame_id="frame-1",
        loader_id="loader-1",
        is_download=False,
    )


@pytest.mark.parametrize(
    "url",
    [
        "",
        "example.test",
        "javascript:alert(1)",
        "file:///etc/passwd",
        "https://example.test/\nheader",
    ],
)
def test_navigation_rejects_non_web_or_malformed_urls(url):
    with pytest.raises(SagasuError) as error:
        navigate.navigate_active_page(
            url,
            client=object(),
        )
    assert error.value.code == "invalid_arguments"
    assert error.value.exit_status == 2


def test_navigation_rejects_lone_unicode_surrogates():
    with pytest.raises(SagasuError) as error:
        navigate.navigate_active_page(
            "https://example.test/\udfff",
            client=object(),
        )

    assert error.value.code == "invalid_arguments"
    assert error.value.exit_status == 2


def test_navigation_reports_browser_failure():
    client = FakeClient(
        {
            "frameId": "frame-1",
            "errorText": "net::ERR_NAME_NOT_RESOLVED",
        }
    )
    with pytest.raises(SagasuError) as error:
        navigate.navigate_active_page(
            "https://does-not-resolve.test/",
            client=client,
        )
    assert error.value.code == "navigation_failed"
    assert (
        error.value.details["reason"] == "net::ERR_NAME_NOT_RESOLVED"
    )
