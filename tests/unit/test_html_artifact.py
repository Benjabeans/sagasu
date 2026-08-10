"""Tests for streamed DOM-document validation."""

from __future__ import annotations

import pytest

from sagasu.artifacts import html
from sagasu.protocol import SagasuError


@pytest.mark.parametrize(
    ("name", "document"),
    [
        (
            "page.html",
            "<!doctype html><html><body><input disabled></body></html>",
        ),
        (
            "image.svg",
            '<svg xmlns="http://www.w3.org/2000/svg"><circle r="5"/></svg>',
        ),
        (
            "feed.xml",
            '<?xml version="1.0" encoding="UTF-8"?>'
            "<feed><title>Updates</title></feed>",
        ),
    ],
)
def test_validate_html_accepts_dom_document_types(tmp_path, name, document):
    path = tmp_path / name
    path.write_text(document, encoding="utf-8")

    assert html.validate_html(path) == len(document.encode("utf-8"))


@pytest.mark.parametrize(
    "document",
    [
        "not a document",
        "<feed><entry></feed>",
        "<!-- no document element -->",
    ],
)
def test_validate_html_rejects_invalid_documents(tmp_path, document):
    path = tmp_path / "invalid.xml"
    path.write_text(document, encoding="utf-8")

    with pytest.raises(SagasuError) as error:
        html.validate_html(path)

    assert error.value.code == "dom_failed"


def test_validate_html_rejects_empty_document(tmp_path):
    path = tmp_path / "empty.xml"
    path.write_bytes(b"")

    with pytest.raises(SagasuError) as error:
        html.validate_html(path)

    assert error.value.code == "dom_failed"
    assert error.value.details["reason"] == "empty document"


def test_validate_html_rejects_oversized_document(monkeypatch, tmp_path):
    path = tmp_path / "large.xml"
    path.write_text("<root/>", encoding="utf-8")
    monkeypatch.setattr(html, "MAX_HTML_BYTES", path.stat().st_size - 1)

    with pytest.raises(SagasuError) as error:
        html.validate_html(path)

    assert error.value.code == "dom_failed"
    assert "document exceeds" in error.value.details["reason"]


def test_validate_html_rejects_invalid_utf8(tmp_path):
    path = tmp_path / "invalid.xml"
    path.write_bytes(b"<root>\xff</root>")

    with pytest.raises(SagasuError) as error:
        html.validate_html(path)

    assert error.value.code == "dom_failed"
    assert error.value.message == "The streamed DOM could not be read"
