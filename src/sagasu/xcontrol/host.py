"""Host-side invocation and safe screenshot publication."""

from __future__ import annotations

import os
import struct
import tempfile
import zlib
from pathlib import Path
from typing import Any, BinaryIO, Sequence

from sagasu.sessions.docker_cli import DockerCLI
from sagasu.sessions.resolver import ResolvedSession
from sagasu.xcontrol.protocol import SagasuError


PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
MAX_PNG_CHUNK = 256 * 1024 * 1024


class SessionXControl:
    def __init__(self, docker: DockerCLI) -> None:
        self.docker = docker

    def invoke(
        self,
        session: ResolvedSession,
        arguments: Sequence[str],
    ) -> dict[str, Any]:
        payload = self.docker.exec_json(session.container_id, arguments)
        _validate_executor_result(payload)
        # Container metadata is host authority. Never trust or preserve values
        # emitted by a process inside the browser container.
        payload["session_id"] = session.session_id
        payload["container_id"] = session.container_id
        return payload

    def screenshot(
        self,
        session: ResolvedSession,
        destination: Path | str,
        *,
        include_pointer: bool,
        overwrite: bool,
    ) -> dict[str, Any]:
        output = Path(destination).expanduser()
        parent = output.parent
        if not parent.is_dir():
            raise SagasuError(
                "invalid_output",
                "The screenshot output directory does not exist",
                {"directory": str(parent)},
                exit_status=2,
            )
        if output.exists() and not overwrite:
            raise SagasuError(
                "output_exists",
                "The screenshot destination already exists; use --overwrite",
                {"path": str(output)},
                exit_status=2,
            )

        temporary_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w+b",
                prefix=f".{output.name}.",
                suffix=".tmp",
                dir=parent,
                delete=False,
            ) as temporary:
                temporary_path = Path(temporary.name)
                arguments = ["screenshot"]
                if not include_pointer:
                    arguments.append("--no-pointer")
                self.docker.exec_stream(
                    session.container_id,
                    arguments,
                    temporary,
                )
                temporary.flush()
                os.fsync(temporary.fileno())

            assert temporary_path is not None
            width, height = validate_png(temporary_path)
            # Recheck after capture so another process cannot be silently
            # overwritten merely because it won the destination race.
            if output.exists() and not overwrite:
                raise SagasuError(
                    "output_exists",
                    "The screenshot destination was created during capture; "
                    "use --overwrite",
                    {"path": str(output)},
                    exit_status=2,
                )
            _publish_screenshot(
                temporary_path,
                output,
                overwrite=overwrite,
            )
            return {
                "ok": True,
                "operation": "screenshot",
                "session_id": session.session_id,
                "container_id": session.container_id,
                "output": str(output),
                "pointer_included": include_pointer,
                "display": {"width": width, "height": height},
            }
        finally:
            if temporary_path is not None:
                try:
                    temporary_path.unlink(missing_ok=True)
                except OSError:
                    pass


def _validate_executor_result(payload: dict[str, Any]) -> None:
    required = ("operation", "backend", "display", "pointer")
    missing = [key for key in required if key not in payload]
    display = payload.get("display")
    pointer = payload.get("pointer")
    if missing or not isinstance(display, dict) or not isinstance(pointer, dict):
        raise SagasuError(
            "invalid_response",
            "sagasu-xcontrol returned an incomplete response",
            {"missing": missing},
        )
    if not _integer_pair(display, "width", "height", positive=True):
        raise SagasuError(
            "invalid_response",
            "sagasu-xcontrol returned invalid display dimensions",
        )
    if not _integer_pair(pointer, "x", "y", positive=False):
        raise SagasuError(
            "invalid_response",
            "sagasu-xcontrol returned an invalid pointer position",
        )


def _publish_screenshot(
    temporary_path: Path,
    output: Path,
    *,
    overwrite: bool,
) -> None:
    try:
        if overwrite:
            os.replace(temporary_path, output)
            return
        # The temporary file is deliberately beside the output, so a hard link
        # is same-filesystem and gives us atomic no-clobber publication. A
        # check followed by os.replace would have a race in which two captures
        # could overwrite each other without --overwrite.
        os.link(temporary_path, output)
    except FileExistsError as exc:
        raise SagasuError(
            "output_exists",
            "The screenshot destination already exists; use --overwrite",
            {"path": str(output)},
            exit_status=2,
        ) from exc
    except OSError as exc:
        raise SagasuError(
            "output_failed",
            "The screenshot could not be published",
            {"path": str(output), "reason": str(exc)},
        ) from exc


def _integer_pair(
    value: dict[str, Any],
    first: str,
    second: str,
    *,
    positive: bool,
) -> bool:
    items = (value.get(first), value.get(second))
    if any(isinstance(item, bool) or not isinstance(item, int) for item in items):
        return False
    if positive:
        return all(item > 0 for item in items)
    return all(item >= 0 for item in items)


def validate_png(path: Path | str) -> tuple[int, int]:
    """Validate PNG signature, chunk framing/CRC, and return IHDR dimensions."""

    png_path = Path(path)
    try:
        with png_path.open("rb") as stream:
            if _read_exact(stream, len(PNG_SIGNATURE)) != PNG_SIGNATURE:
                raise _invalid_png(png_path, "invalid PNG signature")

            width: int | None = None
            height: int | None = None
            saw_idat = False
            saw_iend = False
            chunk_number = 0
            while not saw_iend:
                chunk_number += 1
                length_bytes = _read_exact(stream, 4)
                if len(length_bytes) != 4:
                    raise _invalid_png(png_path, "truncated PNG chunk length")
                length = struct.unpack(">I", length_bytes)[0]
                if length > MAX_PNG_CHUNK:
                    raise _invalid_png(png_path, "PNG chunk is unreasonably large")
                chunk_type = _read_exact(stream, 4)
                if len(chunk_type) != 4:
                    raise _invalid_png(png_path, "truncated PNG chunk type")
                chunk_data = _read_exact(stream, length)
                crc_bytes = _read_exact(stream, 4)
                if len(chunk_data) != length or len(crc_bytes) != 4:
                    raise _invalid_png(png_path, "truncated PNG chunk")
                expected_crc = struct.unpack(">I", crc_bytes)[0]
                actual_crc = zlib.crc32(chunk_type)
                actual_crc = zlib.crc32(chunk_data, actual_crc) & 0xFFFFFFFF
                if actual_crc != expected_crc:
                    raise _invalid_png(
                        png_path,
                        f"invalid CRC in {chunk_type!r} chunk",
                    )

                if chunk_number == 1:
                    if chunk_type != b"IHDR" or length != 13:
                        raise _invalid_png(png_path, "IHDR is not the first chunk")
                    width, height = struct.unpack(">II", chunk_data[:8])
                    if width <= 0 or height <= 0:
                        raise _invalid_png(png_path, "invalid PNG dimensions")
                elif chunk_type == b"IHDR":
                    raise _invalid_png(png_path, "duplicate IHDR chunk")

                if chunk_type == b"IDAT":
                    saw_idat = True
                elif chunk_type == b"IEND":
                    if length != 0:
                        raise _invalid_png(png_path, "invalid IEND chunk")
                    saw_iend = True

            if stream.read(1):
                raise _invalid_png(png_path, "data follows the IEND chunk")
            if width is None or height is None or not saw_idat:
                raise _invalid_png(png_path, "required PNG chunks are missing")
            return width, height
    except SagasuError:
        raise
    except OSError as exc:
        raise SagasuError(
            "capture_failed",
            "The streamed screenshot could not be read",
            {"path": str(png_path), "reason": str(exc)},
        ) from exc


def _read_exact(stream: BinaryIO, length: int) -> bytes:
    chunks: list[bytes] = []
    remaining = length
    while remaining:
        chunk = stream.read(remaining)
        if not chunk:
            break
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def _invalid_png(path: Path, reason: str) -> SagasuError:
    return SagasuError(
        "capture_failed",
        "The session returned an invalid PNG screenshot",
        {"path": str(path), "reason": reason},
    )
