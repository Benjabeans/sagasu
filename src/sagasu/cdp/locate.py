"""Locate a visible page element in the full X-display coordinate space."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Callable, Mapping, Sequence

from sagasu.cdp.client import (
    DEFAULT_TIMEOUT_SECONDS,
    CDPClient,
    CDPSession,
    Connector,
    TargetLoader,
)
from sagasu.cdp.coordinates import (
    BrowserWindowBounds,
    CSSScreenSize,
    CoordinateConversion,
    ViewportMetrics,
    convert_viewport_to_screen,
)
from sagasu.cdp.targets import select_active_page
from sagasu.protocol import SagasuError


MAX_SELECTOR_BYTES = 16 * 1024
_SCREEN_METRICS_EXPRESSION = (
    "({screenWidth:window.screen.width,"
    "screenHeight:window.screen.height,"
    "innerHeight:window.innerHeight,"
    "outerHeight:window.outerHeight})"
)
_Point = tuple[float, float]


@dataclass(frozen=True)
class ElementLocation:
    target_id: str
    title: str
    url: str
    selector: str
    node_id: int
    screen_x: int
    screen_y: int
    viewport_x: float
    viewport_y: float
    viewport_width: float
    viewport_height: float
    viewport_quad: tuple[float, ...]
    visible_polygon: tuple[tuple[float, float], ...]
    viewport_origin_x: float
    viewport_origin_y: float
    scale_x: float
    scale_y: float
    window_left: float
    window_top: float
    window_width: float
    window_height: float


@dataclass(frozen=True)
class _VisibleQuad:
    quad: tuple[float, ...]
    polygon: tuple[tuple[float, float], ...]
    x: float
    y: float
    area: float


def validate_selector(selector: str) -> None:
    """Require a non-empty, bounded selector before contacting the browser."""

    encoded = selector.encode("utf-8")
    if not encoded:
        raise SagasuError(
            "invalid_arguments",
            "Element location requires a CSS selector",
            exit_status=2,
        )
    if len(encoded) > MAX_SELECTOR_BYTES:
        raise SagasuError(
            "invalid_arguments",
            "The CSS selector is too long",
            {"bytes": len(encoded), "max_bytes": MAX_SELECTOR_BYTES},
            exit_status=2,
        )
    if any(ord(character) < 0x20 or ord(character) == 0x7F for character in selector):
        raise SagasuError(
            "invalid_arguments",
            "The CSS selector contains control characters",
            exit_status=2,
        )


def locate_active_element(
    selector: str,
    *,
    display_width: int,
    display_height: int,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    target_loader: TargetLoader | None = None,
    connector: Connector | None = None,
    client: CDPClient | None = None,
) -> ElementLocation:
    """Return a visible top-level element's absolute X-screen location."""

    validate_selector(selector)
    active_client = _client(
        timeout=timeout,
        target_loader=target_loader,
        connector=connector,
        client=client,
    )
    try:
        target = select_active_page(
            active_client.page_targets(),
            client=active_client,
        )
        with active_client.open(target) as session:
            node_id = _query_selector(session, selector)
            viewport = _viewport_metrics(session.call("Page.getLayoutMetrics"))
            visible_quad = _visible_quad(
                session.call("DOM.getContentQuads", {"nodeId": node_id}),
                viewport=viewport,
            )
            window, window_state = _window_bounds(
                session.call(
                    "Browser.getWindowForTarget",
                    {"targetId": target.target_id},
                )
            )
            if window_state == "minimized":
                raise SagasuError(
                    "element_not_visible",
                    "The browser window is minimized",
                )
            css_screen = _css_screen_size(
                session.call(
                    "Runtime.evaluate",
                    {
                        "expression": _SCREEN_METRICS_EXPRESSION,
                        "returnByValue": True,
                    },
                )
            )
        conversion = convert_viewport_to_screen(
            visible_quad.x,
            visible_quad.y,
            window=window,
            viewport=viewport,
            css_screen=css_screen,
            display_width=display_width,
            display_height=display_height,
        )
    except SagasuError as error:
        _translate_transport_error(error)

    return _location_result(
        target_id=target.target_id,
        title=target.title,
        url=target.url,
        selector=selector,
        node_id=node_id,
        viewport=viewport,
        visible_quad=visible_quad,
        window=window,
        conversion=conversion,
    )


def _query_selector(session: CDPSession, selector: str) -> int:
    document = session.call(
        "DOM.getDocument",
        {"depth": 0, "pierce": False},
    )
    root = document.get("root")
    root_id = root.get("nodeId") if isinstance(root, Mapping) else None
    if isinstance(root_id, bool) or not isinstance(root_id, int) or root_id <= 0:
        raise SagasuError(
            "invalid_response",
            "CDP omitted the DOM document node",
        )
    match = session.call(
        "DOM.querySelector",
        {"nodeId": root_id, "selector": selector},
    )
    node_id = match.get("nodeId")
    if isinstance(node_id, bool) or not isinstance(node_id, int):
        raise SagasuError(
            "invalid_response",
            "CDP returned an invalid element node ID",
        )
    if node_id == 0:
        raise SagasuError(
            "element_not_found",
            "No element matches the CSS selector in the active page",
            {"selector": selector},
        )
    return node_id


def _viewport_metrics(response: Mapping[str, object]) -> ViewportMetrics:
    raw = response.get("cssVisualViewport")
    if not isinstance(raw, Mapping):
        raise SagasuError(
            "invalid_response",
            "CDP omitted the CSS visual viewport",
        )
    zoom = (
        _positive_number(raw, "zoom", "viewport zoom")
        if "zoom" in raw
        else 1.0
    )
    return ViewportMetrics(
        width=_positive_number(raw, "clientWidth", "viewport width"),
        height=_positive_number(raw, "clientHeight", "viewport height"),
        zoom=zoom,
    )


def _window_bounds(
    response: Mapping[str, object],
) -> tuple[BrowserWindowBounds, str]:
    raw = response.get("bounds")
    if not isinstance(raw, Mapping):
        raise SagasuError(
            "invalid_response",
            "CDP omitted the browser window bounds",
        )
    state = raw.get("windowState", "normal")
    if not isinstance(state, str):
        raise SagasuError(
            "invalid_response",
            "CDP returned an invalid browser window state",
        )
    return (
        BrowserWindowBounds(
            left=_number(raw, "left", "window left"),
            top=_number(raw, "top", "window top"),
            width=_positive_number(raw, "width", "window width"),
            height=_positive_number(raw, "height", "window height"),
        ),
        state,
    )


def _css_screen_size(response: Mapping[str, object]) -> CSSScreenSize:
    remote_object = response.get("result")
    value = (
        remote_object.get("value")
        if isinstance(remote_object, Mapping)
        else None
    )
    if not isinstance(value, Mapping):
        raise SagasuError(
            "invalid_response",
            "CDP omitted the page's CSS screen dimensions",
        )
    return CSSScreenSize(
        width=_positive_number(value, "screenWidth", "CSS screen width"),
        height=_positive_number(value, "screenHeight", "CSS screen height"),
        inner_height=_positive_number(
            value,
            "innerHeight",
            "browser inner height",
        ),
        outer_height=_positive_number(
            value,
            "outerHeight",
            "browser outer height",
        ),
    )


def _visible_quad(
    response: Mapping[str, object],
    *,
    viewport: ViewportMetrics,
) -> _VisibleQuad:
    raw_quads = response.get("quads")
    if not isinstance(raw_quads, Sequence) or isinstance(
        raw_quads, (str, bytes)
    ):
        raise SagasuError(
            "invalid_response",
            "CDP returned invalid element content quads",
        )

    candidates: list[_VisibleQuad] = []
    for raw_quad in raw_quads:
        quad = _parse_quad(raw_quad)
        points = tuple(zip(quad[::2], quad[1::2]))
        clipped = _clip_to_viewport(
            points,
            width=viewport.width,
            height=viewport.height,
        )
        area = _polygon_area(clipped)
        if area <= 0:
            continue
        x = sum(point[0] for point in clipped) / len(clipped)
        y = sum(point[1] for point in clipped) / len(clipped)
        candidates.append(
            _VisibleQuad(
                quad=quad,
                polygon=clipped,
                x=x,
                y=y,
                area=area,
            )
        )
    if not candidates:
        raise SagasuError(
            "element_not_visible",
            "The matching element has no visible area in the page viewport",
        )
    return max(candidates, key=lambda candidate: candidate.area)


def _parse_quad(value: object) -> tuple[float, ...]:
    if (
        not isinstance(value, Sequence)
        or isinstance(value, (str, bytes))
        or len(value) != 8
    ):
        raise SagasuError(
            "invalid_response",
            "CDP returned a malformed element quad",
        )
    quad: list[float] = []
    for item in value:
        if (
            isinstance(item, bool)
            or not isinstance(item, (int, float))
            or not math.isfinite(item)
        ):
            raise SagasuError(
                "invalid_response",
                "CDP returned a non-numeric element quad",
            )
        quad.append(float(item))
    return tuple(quad)


def _clip_to_viewport(
    polygon: Sequence[_Point],
    *,
    width: float,
    height: float,
) -> tuple[_Point, ...]:
    clipped = list(polygon)
    clipped = _clip_edge(
        clipped,
        inside=lambda point: point[0] >= 0,
        intersect=lambda start, end: _vertical_intersection(start, end, 0),
    )
    clipped = _clip_edge(
        clipped,
        inside=lambda point: point[0] <= width,
        intersect=lambda start, end: _vertical_intersection(
            start, end, width
        ),
    )
    clipped = _clip_edge(
        clipped,
        inside=lambda point: point[1] >= 0,
        intersect=lambda start, end: _horizontal_intersection(start, end, 0),
    )
    clipped = _clip_edge(
        clipped,
        inside=lambda point: point[1] <= height,
        intersect=lambda start, end: _horizontal_intersection(
            start, end, height
        ),
    )
    return tuple(clipped)


def _clip_edge(
    polygon: Sequence[_Point],
    *,
    inside: Callable[[_Point], bool],
    intersect: Callable[[_Point, _Point], _Point],
) -> list[_Point]:
    if not polygon:
        return []
    output: list[_Point] = []
    start = polygon[-1]
    for end in polygon:
        start_inside = inside(start)
        end_inside = inside(end)
        if end_inside:
            if not start_inside:
                output.append(intersect(start, end))
            output.append(end)
        elif start_inside:
            output.append(intersect(start, end))
        start = end
    return output


def _vertical_intersection(
    start: _Point,
    end: _Point,
    x: float,
) -> _Point:
    ratio = (x - start[0]) / (end[0] - start[0])
    return x, start[1] + ratio * (end[1] - start[1])


def _horizontal_intersection(
    start: _Point,
    end: _Point,
    y: float,
) -> _Point:
    ratio = (y - start[1]) / (end[1] - start[1])
    return start[0] + ratio * (end[0] - start[0]), y


def _polygon_area(polygon: Sequence[_Point]) -> float:
    if len(polygon) < 3:
        return 0
    return abs(
        sum(
            start[0] * end[1] - end[0] * start[1]
            for start, end in zip(polygon, (*polygon[1:], polygon[0]))
        )
    ) / 2


def _number(
    value: Mapping[object, object],
    key: str,
    description: str,
) -> float:
    raw = value.get(key)
    if (
        isinstance(raw, bool)
        or not isinstance(raw, (int, float))
        or not math.isfinite(raw)
    ):
        raise SagasuError(
            "invalid_response",
            f"CDP returned an invalid {description}",
        )
    return float(raw)


def _positive_number(
    value: Mapping[object, object],
    key: str,
    description: str,
) -> float:
    result = _number(value, key, description)
    if result <= 0:
        raise SagasuError(
            "invalid_response",
            f"CDP returned a non-positive {description}",
        )
    return result


def _location_result(
    *,
    target_id: str,
    title: str,
    url: str,
    selector: str,
    node_id: int,
    viewport: ViewportMetrics,
    visible_quad: _VisibleQuad,
    window: BrowserWindowBounds,
    conversion: CoordinateConversion,
) -> ElementLocation:
    return ElementLocation(
        target_id=target_id,
        title=title,
        url=url,
        selector=selector,
        node_id=node_id,
        screen_x=conversion.x,
        screen_y=conversion.y,
        viewport_x=visible_quad.x,
        viewport_y=visible_quad.y,
        viewport_width=viewport.width,
        viewport_height=viewport.height,
        viewport_quad=visible_quad.quad,
        visible_polygon=visible_quad.polygon,
        viewport_origin_x=conversion.viewport_origin_x,
        viewport_origin_y=conversion.viewport_origin_y,
        scale_x=conversion.scale_x,
        scale_y=conversion.scale_y,
        window_left=window.left,
        window_top=window.top,
        window_width=window.width,
        window_height=window.height,
    )


def _client(
    *,
    timeout: float,
    target_loader: TargetLoader | None,
    connector: Connector | None,
    client: CDPClient | None,
) -> CDPClient:
    if client is not None and (target_loader is not None or connector is not None):
        raise SagasuError(
            "invalid_arguments",
            "Pass either a CDP client or transport overrides, not both",
            exit_status=2,
        )
    return client or CDPClient(
        timeout=timeout,
        target_loader=target_loader,
        connector=connector,
    )


def _translate_transport_error(error: SagasuError) -> None:
    if error.code not in ("dom_failed", "dom_unavailable"):
        raise error
    raise SagasuError(
        "element_location_failed",
        "CDP could not locate the element in the active page",
        error.details,
    ) from error
