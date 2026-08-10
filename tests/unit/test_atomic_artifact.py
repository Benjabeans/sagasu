from __future__ import annotations

import errno
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest

from sagasu.artifacts import atomic
from sagasu.protocol import SagasuError


def _publish(path, contents):
    return atomic.publish_stream(
        path,
        overwrite=False,
        artifact_name="artifact",
        stream_writer=lambda destination: destination.write(contents),
        validator=lambda temporary, result: (temporary.read_bytes(), result),
    )


def test_no_overwrite_uses_atomic_hardlink_when_supported(
    monkeypatch, tmp_path
):
    def unexpected_fallback(temporary_path, output):
        del temporary_path, output
        raise AssertionError("the reservation fallback should not run")

    monkeypatch.setattr(
        atomic, "_publish_with_reservation", unexpected_fallback
    )
    output = tmp_path / "artifact.bin"

    published = _publish(output, b"complete")

    assert published.path == output
    assert output.read_bytes() == b"complete"
    assert not list(tmp_path.glob(f".{output.name}.*.tmp"))


def test_hardlink_fallback_preserves_no_clobber_under_a_race(
    monkeypatch, tmp_path
):
    publishers_ready = Barrier(2)

    def unsupported_hardlink(source, destination):
        del source, destination
        publishers_ready.wait(timeout=2)
        raise OSError(errno.EOPNOTSUPP, "hard links are not supported")

    monkeypatch.setattr(atomic.os, "link", unsupported_hardlink)
    output = tmp_path / "artifact.bin"

    def publish(contents):
        try:
            _publish(output, contents)
            return "ok"
        except SagasuError as error:
            return error.code

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(publish, (b"first", b"second")))

    assert sorted(outcomes) == ["ok", "output_exists"]
    assert output.read_bytes() in (b"first", b"second")
    assert not list(tmp_path.glob(f".{output.name}.*.tmp"))


def test_hardlink_fallback_removes_reservation_when_replace_fails(
    monkeypatch, tmp_path
):
    def unsupported_hardlink(source, destination):
        del source, destination
        raise OSError(errno.EOPNOTSUPP, "hard links are not supported")

    def failed_replace(source, destination):
        del source, destination
        raise OSError(errno.EIO, "replacement failed")

    monkeypatch.setattr(atomic.os, "link", unsupported_hardlink)
    monkeypatch.setattr(atomic.os, "replace", failed_replace)
    output = tmp_path / "artifact.bin"

    with pytest.raises(SagasuError) as error:
        _publish(output, b"complete")

    assert error.value.code == "output_failed"
    assert not output.exists()
    assert not list(tmp_path.glob(f".{output.name}.*.tmp"))
