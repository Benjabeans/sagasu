"""Small, argument-array-only wrapper around the Docker CLI."""

from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass
from typing import Any, BinaryIO, Callable, Mapping, Sequence

from sagasu.xcontrol.protocol import SagasuError, parse_json_object


SESSION_LABEL = "computer.sagasu.session.id"


@dataclass(frozen=True)
class ContainerSummary:
    container_id: str
    name: str
    state: str
    labels: Mapping[str, str]


def _parse_labels(value: Any) -> dict[str, str]:
    if isinstance(value, Mapping):
        return {str(key): str(item) for key, item in value.items()}
    if not isinstance(value, str) or not value:
        return {}
    labels: dict[str, str] = {}
    for part in value.split(","):
        key, separator, item = part.partition("=")
        if separator:
            labels[key] = item
        elif key:
            labels[key] = ""
    return labels


class DockerCLI:
    """Docker transport with injectable process functions for unit tests."""

    def __init__(
        self,
        executable: str | None = None,
        *,
        runner: Callable[..., subprocess.CompletedProcess[bytes]] = subprocess.run,
        popen: Callable[..., subprocess.Popen[bytes]] = subprocess.Popen,
    ) -> None:
        self.executable = executable or os.environ.get(
            "SAGASU_DOCKER_CLI", "docker"
        )
        self._runner = runner
        self._popen = popen

    def _command(self, *arguments: str) -> list[str]:
        return [self.executable, *arguments]

    def _run(self, arguments: Sequence[str]) -> subprocess.CompletedProcess[bytes]:
        command = self._command(*arguments)
        try:
            completed = self._runner(
                command,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
        except FileNotFoundError as exc:
            raise SagasuError(
                "docker_unavailable",
                f"Docker executable {self.executable!r} was not found",
            ) from exc
        except OSError as exc:
            raise SagasuError(
                "docker_unavailable",
                "Docker could not be started",
                {"reason": str(exc)},
            ) from exc
        return completed

    @staticmethod
    def _stderr(completed: subprocess.CompletedProcess[bytes]) -> str:
        return completed.stderr.decode("utf-8", errors="replace").strip()

    def containers_for_session(self, session_id: str) -> list[ContainerSummary]:
        completed = self._run(
            [
                "container",
                "ls",
                "--all",
                "--filter",
                f"label={SESSION_LABEL}={session_id}",
                "--format",
                "{{json .}}",
            ]
        )
        if completed.returncode:
            raise SagasuError(
                "docker_failed",
                "Docker could not list Sagasu sessions",
                {"reason": self._stderr(completed)},
            )

        containers: list[ContainerSummary] = []
        output = completed.stdout.decode("utf-8", errors="strict")
        for line_number, line in enumerate(output.splitlines(), start=1):
            if not line.strip():
                continue
            try:
                value = json.loads(line)
            except json.JSONDecodeError as exc:
                raise SagasuError(
                    "invalid_response",
                    "Docker returned an invalid container listing",
                    {"line": line_number, "reason": str(exc)},
                ) from exc
            if not isinstance(value, Mapping):
                raise SagasuError(
                    "invalid_response",
                    "Docker returned a non-object container listing",
                    {"line": line_number},
                )
            container_id = str(value.get("ID") or value.get("Id") or "")
            name = str(value.get("Names") or value.get("Name") or "")
            state = str(value.get("State") or "")
            if not container_id:
                raise SagasuError(
                    "invalid_response",
                    "Docker omitted a container ID from its listing",
                    {"line": line_number},
                )
            containers.append(
                ContainerSummary(
                    container_id=container_id,
                    name=name,
                    state=state,
                    labels=_parse_labels(value.get("Labels")),
                )
            )
        return containers

    def inspect_container(self, name: str) -> Mapping[str, Any]:
        completed = self._run(["container", "inspect", name])
        if completed.returncode:
            reason = self._stderr(completed)
            lowered = reason.casefold()
            if "no such container" in lowered or "no such object" in lowered:
                raise SagasuError(
                    "session_not_found",
                    f"No container named {name!r} exists",
                    {"container": name},
                )
            raise SagasuError(
                "docker_failed",
                f"Docker could not inspect container {name!r}",
                {"reason": reason},
            )
        payload = parse_json_object_or_array(completed.stdout, source="Docker inspect")
        if not isinstance(payload, list) or len(payload) != 1:
            raise SagasuError(
                "invalid_response",
                "Docker inspect did not return exactly one container",
                {"container": name},
            )
        item = payload[0]
        if not isinstance(item, Mapping):
            raise SagasuError(
                "invalid_response",
                "Docker inspect returned an invalid container object",
                {"container": name},
            )
        return item

    @staticmethod
    def _exec_arguments(container_id: str, arguments: Sequence[str]) -> list[str]:
        # Deliberately omit -i and -t. In particular, a TTY corrupts raw PNG
        # bytes on Docker implementations that perform terminal translation.
        return [
            "exec",
            "--user",
            "sagasu",
            container_id,
            "sagasu-xcontrol",
            *arguments,
        ]

    def exec_json(
        self, container_id: str, arguments: Sequence[str]
    ) -> dict[str, Any]:
        completed = self._run(self._exec_arguments(container_id, arguments))
        if completed.returncode:
            error = _executor_error(completed.stderr)
            if error is not None:
                raise error
            raise SagasuError(
                "input_failed",
                "The in-container X-control command failed",
                {
                    "container_id": container_id,
                    "reason": self._stderr(completed),
                },
            )
        payload = parse_json_object(completed.stdout, source="sagasu-xcontrol")
        if payload.get("ok") is not True:
            raise SagasuError.from_payload(payload)
        return payload

    def exec_stream(
        self,
        container_id: str,
        arguments: Sequence[str],
        destination: BinaryIO,
    ) -> None:
        command = self._command(*self._exec_arguments(container_id, arguments))
        try:
            process = self._popen(
                command,
                stdin=subprocess.DEVNULL,
                stdout=destination,
                stderr=subprocess.PIPE,
            )
            _, stderr = process.communicate()
        except FileNotFoundError as exc:
            raise SagasuError(
                "docker_unavailable",
                f"Docker executable {self.executable!r} was not found",
            ) from exc
        except OSError as exc:
            raise SagasuError(
                "docker_unavailable",
                "Docker could not stream a session screenshot",
                {"reason": str(exc)},
            ) from exc
        if process.returncode:
            error = _executor_error(stderr or b"")
            if error is not None:
                raise error
            raise SagasuError(
                "capture_failed",
                "The in-container screenshot command failed",
                {
                    "container_id": container_id,
                    "reason": (stderr or b"").decode(
                        "utf-8", errors="replace"
                    ).strip(),
                },
            )


def parse_json_object_or_array(data: bytes, *, source: str) -> object:
    try:
        return json.loads(data)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SagasuError(
            "invalid_response",
            f"{source} returned invalid JSON",
            {"reason": str(exc)},
        ) from exc


def _executor_error(stderr: bytes) -> SagasuError | None:
    """Extract the executor's final JSON error while tolerating tool warnings."""

    for line in reversed(stderr.decode("utf-8", errors="replace").splitlines()):
        if not line.strip():
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, Mapping) and payload.get("ok") is False:
            return SagasuError.from_payload(payload)
    return None

