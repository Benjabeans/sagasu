"""Value objects and metadata shared by session discovery and execution."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping


SESSION_LABEL = "computer.sagasu.session.id"


@dataclass(frozen=True)
class ContainerSummary:
    container_id: str
    name: str
    state: str
    labels: Mapping[str, str]


@dataclass(frozen=True)
class ResolvedSession:
    session_id: str | None
    container_id: str
    container_name: str


def parse_labels(value: Any) -> dict[str, str]:
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

