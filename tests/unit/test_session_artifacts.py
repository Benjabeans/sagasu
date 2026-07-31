from __future__ import annotations

import struct
import zlib
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest

from sagasu.artifacts.html import validate_html
from sagasu.artifacts.png import validate_png
from sagasu.protocol import SagasuError
from sagasu.sessions.artifacts import save_dom, save_screenshot
from sagasu.sessions.executor import SessionExecutor
from sagasu.sessions.models import ResolvedSession


def png(width=2, height=3):
    def chunk(kind, data):
        crc = zlib.crc32(kind)
        crc = zlib.crc32(data, crc) & 0xFFFFFFFF
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", crc)

    scanlines = b"".join(b"\x00" + b"\x00\x00\x00" * width for _ in range(height))
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", ihdr)
        + chunk(b"IDAT", zlib.compress(scanlines))
        + chunk(b"IEND", b"")
    )


class FakeDocker:
    def __init__(self, image=None, error=None):
        self.image = image if image is not None else png()
        self.error = error
        self.stream_names = []
        self.stream_arguments = []

    def exec_stream(self, container_id, arguments, destination):
        self.stream_names.append(destination.name)
        self.stream_arguments.append((container_id, list(arguments)))
        destination.write(self.image)
        if self.error:
            raise self.error

    def exec_json(self, container_id, arguments):
        return {
            "ok": True,
            "operation": "cursor.move",
            "backend": "humancursor",
            "display": {"width": 100, "height": 80},
            "pointer": {"x": 10, "y": 20},
        }

    def exec_stream_json(self, container_id, arguments, destination):
        document = "<!doctype html><html><body>live</body></html>".encode()
        destination.write(document)
        return {
            "ok": True,
            "operation": "dom.fetch",
            "backend": "cdp",
            "display": {"width": 100, "height": 80},
            "pointer": {"x": 10, "y": 20},
            "target_id": "target-1",
            "title": "Live",
            "url": "https://example.test/",
            "bytes": len(document),
        }


SESSION = ResolvedSession(
    session_id="6f1c908d-2acc-4a1e-85f6-0f1b96857672",
    container_id="abc123",
    container_name="session",
)


def test_invoke_adds_host_authoritative_session_metadata():
    payload = SessionExecutor(FakeDocker(), SESSION).invoke(
        ["cursor", "move"]
    )
    assert payload["session_id"] == SESSION.session_id
    assert payload["container_id"] == SESSION.container_id
    assert payload["backend"] == "humancursor"


def test_screenshot_validates_and_atomically_publishes(tmp_path):
    docker = FakeDocker(png(7, 9))
    output = tmp_path / "screen.png"
    payload = save_screenshot(
        SessionExecutor(docker, SESSION),
        output,
        include_pointer=True,
        overwrite=False,
    )
    assert output.read_bytes() == png(7, 9)
    assert payload["display"] == {"width": 7, "height": 9}
    assert payload["pointer_included"] is True
    assert docker.stream_arguments == [
        ("abc123", ["screenshot"])
    ]
    assert not list(tmp_path.glob("*.tmp"))


def test_no_pointer_is_forwarded(tmp_path):
    docker = FakeDocker()
    save_screenshot(
        SessionExecutor(docker, SESSION),
        tmp_path / "screen.png",
        include_pointer=False,
        overwrite=False,
    )
    assert docker.stream_arguments[0][1] == ["screenshot", "--no-pointer"]


def test_existing_output_requires_overwrite(tmp_path):
    output = tmp_path / "screen.png"
    output.write_bytes(b"old")
    with pytest.raises(SagasuError) as error:
        save_screenshot(
            SessionExecutor(FakeDocker(), SESSION),
            output,
            include_pointer=True,
            overwrite=False,
        )
    assert error.value.code == "output_exists"
    assert output.read_bytes() == b"old"

    save_screenshot(
        SessionExecutor(FakeDocker(), SESSION),
        output,
        include_pointer=True,
        overwrite=True,
    )
    assert output.read_bytes().startswith(b"\x89PNG")


def test_failed_or_invalid_capture_leaves_no_partial_destination(tmp_path):
    for docker in (
        FakeDocker(image=b"not png"),
        FakeDocker(error=SagasuError("capture_failed", "boom")),
    ):
        output = tmp_path / f"{id(docker)}.png"
        with pytest.raises(SagasuError):
            save_screenshot(
                SessionExecutor(docker, SESSION),
                output,
                include_pointer=True,
                overwrite=False,
            )
        assert not output.exists()
        assert not list(tmp_path.glob(f".{output.name}.*.tmp"))


def test_concurrent_captures_use_unique_temporary_names(tmp_path):
    docker = FakeDocker()
    session_executor = SessionExecutor(docker, SESSION)

    def capture(number):
        return save_screenshot(
            session_executor,
            tmp_path / f"{number}.png",
            include_pointer=True,
            overwrite=False,
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(capture, (1, 2)))
    assert len(set(docker.stream_names)) == 2


def test_concurrent_no_overwrite_publication_has_one_winner(tmp_path):
    barrier = Barrier(2)

    class BarrierDocker(FakeDocker):
        def exec_stream(self, container_id, arguments, destination):
            barrier.wait(timeout=2)
            super().exec_stream(container_id, arguments, destination)

    docker = BarrierDocker()
    session_executor = SessionExecutor(docker, SESSION)
    output = tmp_path / "shared.png"

    def capture():
        try:
            save_screenshot(
                session_executor,
                output,
                include_pointer=True,
                overwrite=False,
            )
            return "ok"
        except SagasuError as error:
            return error.code

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: capture(), (1, 2)))
    assert sorted(results) == ["ok", "output_exists"]
    assert output.read_bytes().startswith(b"\x89PNG")


def test_png_crc_is_validated(tmp_path):
    path = tmp_path / "bad.png"
    data = bytearray(png())
    data[-5] ^= 1
    path.write_bytes(data)
    with pytest.raises(SagasuError) as error:
        validate_png(path)
    assert error.value.code == "capture_failed"


def test_dom_validates_and_atomically_publishes(tmp_path):
    output = tmp_path / "page.html"
    payload = save_dom(
        SessionExecutor(FakeDocker(), SESSION),
        output,
        overwrite=False,
    )
    assert output.read_text() == (
        "<!doctype html><html><body>live</body></html>"
    )
    assert payload["operation"] == "dom.fetch"
    assert payload["backend"] == "cdp"
    assert payload["session_id"] == SESSION.session_id
    assert payload["container_id"] == SESSION.container_id
    assert payload["output"] == str(output)


def test_invalid_dom_metadata_leaves_no_destination(tmp_path):
    class InvalidMetadataDocker(FakeDocker):
        def exec_stream_json(self, container_id, arguments, destination):
            payload = super().exec_stream_json(
                container_id, arguments, destination
            )
            payload["bytes"] += 1
            return payload

    output = tmp_path / "page.html"
    with pytest.raises(SagasuError) as error:
        save_dom(
            SessionExecutor(InvalidMetadataDocker(), SESSION),
            output,
            overwrite=False,
        )
    assert error.value.code == "invalid_response"
    assert not output.exists()
    assert not list(tmp_path.glob(f".{output.name}.*.tmp"))


def test_invalid_dom_is_rejected(tmp_path):
    path = tmp_path / "bad.html"
    path.write_text("not a document")
    with pytest.raises(SagasuError) as error:
        validate_html(path)
    assert error.value.code == "dom_failed"
