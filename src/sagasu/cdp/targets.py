"""Select the visible page target shared by CDP browser operations."""

from __future__ import annotations

from typing import Mapping, Sequence

from sagasu.cdp.client import CDPClient, PageTarget
from sagasu.protocol import SagasuError


def select_active_page(
    targets: Sequence[PageTarget],
    *,
    client: CDPClient,
) -> PageTarget:
    """Return the sole visible page, or fail rather than guessing."""

    if len(targets) == 1:
        return targets[0]

    visible: list[PageTarget] = []
    for target in targets:
        with client.open(target) as session:
            result = session.call(
                "Runtime.evaluate",
                {
                    "expression": "document.visibilityState",
                    "returnByValue": True,
                },
            )
        remote_object = result.get("result")
        if (
            isinstance(remote_object, Mapping)
            and remote_object.get("value") == "visible"
        ):
            visible.append(target)

    if len(visible) == 1:
        return visible[0]
    if not visible:
        raise SagasuError(
            "dom_target_not_found",
            "No browser page is currently visible",
            {"page_targets": len(targets)},
        )
    raise SagasuError(
        "dom_target_ambiguous",
        "More than one browser page is currently visible",
        {"target_ids": [target.target_id for target in visible]},
    )
