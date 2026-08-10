"""Safely publish a streamed artifact without exposing partial output."""

from __future__ import annotations

import errno
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, Callable, Generic, TypeVar

from sagasu.protocol import SagasuError


StreamResult = TypeVar("StreamResult")
ValidationResult = TypeVar("ValidationResult")


_HARDLINK_UNSUPPORTED_ERRNOS = frozenset(
    {
        errno.EPERM,
        errno.EXDEV,
        errno.ENOSYS,
        errno.EOPNOTSUPP,
        errno.ENOTSUP,
    }
)


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


def publish_reserved_stream(
    destination: Path | str,
    *,
    overwrite: bool,
    artifact_name: str,
    stream_writer: Callable[[BinaryIO], StreamResult],
    validator: Callable[[Path, StreamResult], ValidationResult],
) -> PublishedArtifact[StreamResult, ValidationResult]:
    """Reserve a no-overwrite destination before streaming mutations.

    ``publish_stream`` is appropriate for observations because losing a
    publication race only means that the observation can be repeated.  A
    mutating stream cannot be retried safely.  With no-overwrite semantics,
    this variant therefore creates and owns the destination placeholder before
    calling ``stream_writer`` and replaces it only while it is still the same
    filesystem object.

    Explicit overwrite keeps the established replace behavior: an existing
    destination is intentionally not available for exclusive reservation.
    The temporary file is still created before ``stream_writer`` is called.
    """

    if overwrite:
        return publish_stream(
            destination,
            overwrite=True,
            artifact_name=artifact_name,
            stream_writer=stream_writer,
            validator=validator,
        )

    output = Path(destination).expanduser()
    parent = output.parent
    if not parent.is_dir():
        raise SagasuError(
            "invalid_output",
            f"The {artifact_name} output directory does not exist",
            {"directory": str(parent)},
            exit_status=2,
        )

    reservation = _reserve_destination(output, artifact_name=artifact_name)
    temporary_path: Path | None = None
    try:
        try:
            temporary = tempfile.NamedTemporaryFile(
                mode="w+b",
                prefix=f".{output.name}.",
                suffix=".tmp",
                dir=parent,
                delete=False,
            )
        except OSError as exc:
            raise SagasuError(
                "output_failed",
                f"The {artifact_name} temporary output could not be created",
                {"path": str(output), "reason": str(exc)},
            ) from exc

        with temporary:
            temporary_path = Path(temporary.name)
            stream_result = stream_writer(temporary)
            temporary.flush()
            os.fsync(temporary.fileno())

        validation = validator(temporary_path, stream_result)
        _publish_over_reservation(
            temporary_path,
            reservation,
            artifact_name=artifact_name,
        )
        return PublishedArtifact(
            path=output,
            stream_result=stream_result,
            validation=validation,
        )
    finally:
        # Both cleanup operations are identity-aware or target a unique path.
        # In particular, never unlink a destination another process installed
        # after displacing our reservation.
        _remove_reservation(output, reservation.identity)
        try:
            os.close(reservation.descriptor)
        except OSError:
            pass
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
        try:
            os.link(temporary_path, output)
        except OSError as exc:
            if exc.errno not in _HARDLINK_UNSUPPORTED_ERRNOS:
                raise
            _publish_with_reservation(temporary_path, output)
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


def _publish_with_reservation(temporary_path: Path, output: Path) -> None:
    """Publish without clobbering when the filesystem cannot hard-link."""

    reservation = _create_reservation(output)
    try:
        _replace_owned_reservation(temporary_path, reservation)
    finally:
        _remove_reservation(output, reservation.identity)
        try:
            os.close(reservation.descriptor)
        except OSError:
            pass


@dataclass(frozen=True)
class _DestinationReservation:
    """An exclusively created output placeholder kept alive by its fd."""

    path: Path
    descriptor: int
    identity: os.stat_result


def _create_reservation(output: Path) -> _DestinationReservation:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC

    descriptor = os.open(output, flags, 0o600)
    try:
        identity = os.fstat(descriptor)
    except BaseException:
        try:
            os.close(descriptor)
        except OSError:
            pass
        raise
    return _DestinationReservation(output, descriptor, identity)


def _reserve_destination(
    output: Path, *, artifact_name: str
) -> _DestinationReservation:
    try:
        return _create_reservation(output)
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
            f"The {artifact_name} destination could not be reserved",
            {"path": str(output), "reason": str(exc)},
        ) from exc


def _publish_over_reservation(
    temporary_path: Path,
    reservation: _DestinationReservation,
    *,
    artifact_name: str,
) -> None:
    try:
        _replace_owned_reservation(temporary_path, reservation)
    except FileExistsError as exc:
        raise SagasuError(
            "output_exists",
            f"The {artifact_name} destination changed during capture",
            {"path": str(reservation.path)},
            exit_status=2,
        ) from exc
    except OSError as exc:
        raise SagasuError(
            "output_failed",
            f"The {artifact_name} could not be published",
            {"path": str(reservation.path), "reason": str(exc)},
        ) from exc


def _replace_owned_reservation(
    temporary_path: Path, reservation: _DestinationReservation
) -> None:
    try:
        current_stat = reservation.path.stat(follow_symlinks=False)
    except FileNotFoundError as exc:
        raise OSError(
            errno.ENOENT,
            "the destination reservation disappeared",
            str(reservation.path),
        ) from exc
    if not os.path.samestat(current_stat, reservation.identity):
        raise FileExistsError(
            errno.EEXIST,
            "the destination reservation was replaced",
            str(reservation.path),
        )

    # Replacing our exclusively created placeholder is atomic, so readers see
    # either the reservation or the complete validated artifact.
    os.replace(temporary_path, reservation.path)


def _remove_reservation(output: Path, reservation_stat: os.stat_result) -> None:
    """Remove only the reservation created by this publisher."""

    try:
        current_stat = output.stat(follow_symlinks=False)
        if os.path.samestat(current_stat, reservation_stat):
            output.unlink()
    except FileNotFoundError:
        pass
    except OSError:
        # Preserve the original publication failure. A changed destination is
        # intentionally left alone; an inaccessible reservation is best-effort
        # cleanup for the same reason temporary cleanup is best-effort above.
        pass
