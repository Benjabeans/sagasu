"""Strict validation for streamed PNG screenshots."""

from __future__ import annotations

import struct
import zlib
from pathlib import Path
from typing import BinaryIO

from sagasu.protocol import SagasuError


PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
MAX_PNG_CHUNK = 256 * 1024 * 1024


def validate_png(path: Path | str) -> tuple[int, int]:
    """Validate PNG signature, chunk framing/CRC, and return dimensions."""

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
                    raise _invalid_png(
                        png_path, "PNG chunk is unreasonably large"
                    )
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
                        raise _invalid_png(
                            png_path, "IHDR is not the first chunk"
                        )
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
                raise _invalid_png(
                    png_path, "required PNG chunks are missing"
                )
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

