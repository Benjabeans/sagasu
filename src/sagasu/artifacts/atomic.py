"""Safely publish a streamed artifact without exposing partial output."""

from __future__ import annotations

import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, Callable, Generic, TypeVar

from sagasu.protocol import SagasuError


StreamResult = TypeVar("StreamResult")
ValidationResult = TypeVar("ValidationResult")


@dataclass(frozen=True)
class PublishedArtifact(Generic[StreamResult, ValidationResult]):
    """The results of streaming, validating, and publishing one artifact."""

    path: Path
    stream_result: StreamResult
    validation: ValidationResult


def publish_stream(
    destination: Path | str,
    *,
    overwrite: bool,
    artifact_name: str,
    stream_writer: Callable[[BinaryIO], StreamResult],
    validator: Callable[[Path, StreamResult], ValidationResult],
) -> PublishedArtifact[StreamResult, ValidationResult]:
    """Stream beside ``destination``, validate, then publish atomically."""

    output = Path(destination).expanduser()
    parent = output.parent
    if not parent.is_dir():
        raise SagasuError(
            "invalid_output",
            f"The {artifact_name} output directory does not exist",
            {"directory": str(parent)},
            exit_status=2,
        )
    _ensure_available(output, overwrite=overwrite, artifact_name=artifact_name)

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
            stream_result = stream_writer(temporary)
            temporary.flush()
            os.fsync(temporary.fileno())

        validation = validator(temporary_path, stream_result)
        # Recheck after the potentially long stream so a concurrent producer
        # cannot be silently overwritten without explicit permission.
        _ensure_available(
            output,
            overwrite=overwrite,
            artifact_name=artifact_name,
            during_capture=True,
        )
        _publish(
            temporary_path,
            output,
            overwrite=overwrite,
            artifact_name=artifact_name,
        )
        return PublishedArtifact(
            path=output,
            stream_result=stream_result,
            validation=validation,
        )
    finally:
        if temporary_path is not None:
            try:
                temporary_path.unlink(missing_ok=True)
            except OSError:
                pass


def _ensure_available(
    output: Path,
    *,
    overwrite: bool,
    artifact_name: str,
    during_capture: bool = False,
) -> None:
    if overwrite or not output.exists():
        return
    timing = " was created during capture" if during_capture else " already exists"
    raise SagasuError(
        "output_exists",
        f"The {artifact_name} destination{timing}; use --overwrite",
        {"path": str(output)},
        exit_status=2,
    )


def _publish(
    temporary_path: Path,
    output: Path,
    *,
    overwrite: bool,
    artifact_name: str,
) -> None:
    try:
        if overwrite:
            os.replace(temporary_path, output)
            return
        # The temporary file is deliberately beside the output, so this hard
        # link is same-filesystem and provides atomic no-clobber publication.
        os.link(temporary_path, output)
    except FileExistsError as exc:
        raise SagasuError(
            "output_exists",
            f"The {artifact_name} destination already exists; use --overwrite",
            {"path": str(output)},
            exit_status=2,
        ) from exc
    except OSError as exc:
        raise SagasuError(
            "output_failed",
            f"The {artifact_name} could not be published",
            {"path": str(output), "reason": str(exc)},
        ) from exc
