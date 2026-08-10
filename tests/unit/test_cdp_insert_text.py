from __future__ import annotations

import pytest

from sagasu.cdp import insert_text
from sagasu.cdp.client import PageTarget
from sagasu.protocol import SagasuError


TARGET = PageTarget(
    target_id="target-1",
    title="Example",
    url="https://example.test/current",
    websocket_url="ws://127.0.0.1:9222/devtools/page/target-1",
)


class FakeSession:
    def __init__(self):
        self.calls = []

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        pass

    def call(self, method, params=None):
        self.calls.append((method, params or {}))
        return {}


class FakeClient:
    def __init__(self):
        self.session = FakeSession()

    def page_targets(self):
        return [TARGET]

    def open(self, target):
        assert target == TARGET
        return self.session


def test_insert_text_uses_cdp_without_echoing_text_in_result():
    client = FakeClient()
    text = "有線 IEM 🎧"
    result = insert_text.insert_text_active_page(text, client=client)
    assert client.session.calls == [("Input.insertText", {"text": text})]
    assert result.target_id == "target-1"
    assert result.character_count == len(text)
    assert result.byte_count == len(text.encode("utf-8"))
    assert not hasattr(result, "text")


def test_insert_text_rejects_empty_and_oversized_values(monkeypatch):
    with pytest.raises(SagasuError) as empty:
        insert_text.insert_text_active_page(
            "",
            client=object(),
        )
    assert empty.value.code == "invalid_arguments"

    monkeypatch.setattr(insert_text, "MAX_INSERT_TEXT_BYTES", 3)
    with pytest.raises(SagasuError) as oversized:
        insert_text.insert_text_active_page(
            "文字",
            client=object(),
        )
    assert oversized.value.code == "invalid_arguments"


def test_insert_text_rejects_lone_unicode_surrogates():
    with pytest.raises(SagasuError) as error:
        insert_text.insert_text_active_page(
            "\ud800",
            client=object(),
        )

    assert error.value.code == "invalid_arguments"
    assert error.value.exit_status == 2
