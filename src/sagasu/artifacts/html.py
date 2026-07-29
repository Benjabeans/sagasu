"""Validation for streamed live-DOM HTML documents."""

from __future__ import annotations

import re
from pathlib import Path

from sagasu.protocol import SagasuError


MAX_HTML_BYTES = 64 * 1024 * 1024


def validate_html(path: Path | str) -> int:
    """Validate a streamed UTF-8 HTML document and return its byte count."""

    html_path = Path(path)
    try:
        byte_count = html_path.stat().st_size
        if byte_count <= 0:
            raise _invalid_html(html_path, "empty document")
        if byte_count > MAX_HTML_BYTES:
            raise _invalid_html(
                html_path,
                f"document exceeds {MAX_HTML_BYTES} bytes",
            )
        text = html_path.read_text(encoding="utf-8")
        if re.search(r"<html(?:\s|>)", text, flags=re.IGNORECASE) is None:
            raise _invalid_html(html_path, "missing HTML document element")
        return byte_count
    except SagasuError:
        raise
    except (OSError, UnicodeDecodeError) as exc:
        raise SagasuError(
            "dom_failed",
            "The streamed DOM could not be read",
            {"path": str(html_path), "reason": str(exc)},
        ) from exc


def _invalid_html(path: Path, reason: str) -> SagasuError:
    return SagasuError(
        "dom_failed",
        "The session returned an invalid HTML DOM",
        {"path": str(path), "reason": reason},
    )

