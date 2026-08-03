"""Convert page-viewport CSS coordinates into full X-display pixels."""

from __future__ import annotations

import math
from dataclasses import dataclass

from sagasu.protocol import SagasuError


@dataclass(frozen=True)
class BrowserWindowBounds:
    """Browser window bounds in the coordinate space reported by CDP."""

    left: float
    top: float
    width: float
    height: float


@dataclass(frozen=True)
class ViewportMetrics:
    """Visible page viewport dimensions and its CSS-to-window zoom."""

    width: float
    height: float
    zoom: float


@dataclass(frozen=True)
class CSSScreenSize:
    """Screen and browser dimensions exposed by the CSSOM View API."""

    width: float
    height: float
    inner_height: float
    outer_height: float


@dataclass(frozen=True)
class CoordinateConversion:
    """One absolute screen point plus the transform used to derive it."""

    x: int
    y: int
    viewport_origin_x: float
    viewport_origin_y: float
    scale_x: float
    scale_y: float


def convert_viewport_to_screen(
    viewport_x: float,
    viewport_y: float,
    *,
    window: BrowserWindowBounds,
    viewport: ViewportMetrics,
    css_screen: CSSScreenSize,
    display_width: int,
    display_height: int,
) -> CoordinateConversion:
    """Map a visible viewport point to the matching full-display pixel.

    Chromium reports element quads in viewport CSS pixels, browser bounds in
    screen coordinates, and page zoom as the CSS-to-window scale. Sagasu's X
    screenshot uses root-display pixels. CSSOM ``innerHeight`` includes a
    horizontal scrollbar, unlike CDP's viewport ``clientHeight``. Using the
    inner browser height therefore keeps the viewport's top edge stable when
    bottom scrollbar space appears.
    """

    _require_display_size(display_width, display_height)
    _require_finite("viewport_x", viewport_x)
    _require_finite("viewport_y", viewport_y)
    _require_positive("window.width", window.width)
    _require_positive("window.height", window.height)
    _require_finite("window.left", window.left)
    _require_finite("window.top", window.top)
    _require_positive("viewport.width", viewport.width)
    _require_positive("viewport.height", viewport.height)
    _require_positive("viewport.zoom", viewport.zoom)
    _require_positive("css_screen.width", css_screen.width)
    _require_positive("css_screen.height", css_screen.height)
    _require_positive("css_screen.inner_height", css_screen.inner_height)
    _require_positive("css_screen.outer_height", css_screen.outer_height)

    if not 0 <= viewport_x < viewport.width or not 0 <= viewport_y < viewport.height:
        raise SagasuError(
            "coordinate_mapping_failed",
            "The viewport point is outside the visible page",
            {
                "point": {"x": viewport_x, "y": viewport_y},
                "viewport": {
                    "width": viewport.width,
                    "height": viewport.height,
                },
            },
        )

    if viewport.height > css_screen.inner_height + 1:
        raise SagasuError(
            "coordinate_mapping_failed",
            "The CDP viewport is taller than the browser's inner viewport",
            {
                "viewport_height": viewport.height,
                "browser_inner_height": css_screen.inner_height,
            },
        )

    viewport_window_width = viewport.width * viewport.zoom
    viewport_window_height = viewport.height * viewport.zoom
    inner_window_height = css_screen.inner_height * viewport.zoom
    if inner_window_height > css_screen.outer_height + 1:
        raise SagasuError(
            "coordinate_mapping_failed",
            "The browser's inner viewport is taller than its outer window",
            {
                "browser_inner_height": inner_window_height,
                "browser_outer_height": css_screen.outer_height,
            },
        )
    if (
        viewport_window_width > window.width + 1
        or viewport_window_height > window.height + 1
    ):
        raise SagasuError(
            "coordinate_mapping_failed",
            "The CDP viewport is larger than its browser window",
            {
                "viewport_in_window": {
                    "width": viewport_window_width,
                    "height": viewport_window_height,
                },
                "window": {
                    "width": window.width,
                    "height": window.height,
                },
            },
        )

    screen_scale_x = display_width / css_screen.width
    screen_scale_y = display_height / css_screen.height
    point_scale_x = viewport.zoom * screen_scale_x
    point_scale_y = viewport.zoom * screen_scale_y

    origin_x = window.left * screen_scale_x
    top_chrome_height = css_screen.outer_height - inner_window_height
    window_height_scale = window.height / css_screen.outer_height
    origin_y = (
        window.top + top_chrome_height * window_height_scale
    ) * screen_scale_y
    screen_x_float = origin_x + viewport_x * point_scale_x
    screen_y_float = origin_y + viewport_y * point_scale_y
    screen_x = _nearest_pixel(screen_x_float)
    screen_y = _nearest_pixel(screen_y_float)

    if not 0 <= screen_x < display_width or not 0 <= screen_y < display_height:
        raise SagasuError(
            "coordinate_mapping_failed",
            "The converted point is outside the X display",
            {
                "coordinate": {"x": screen_x, "y": screen_y},
                "display": {
                    "width": display_width,
                    "height": display_height,
                },
            },
        )

    return CoordinateConversion(
        x=screen_x,
        y=screen_y,
        viewport_origin_x=origin_x,
        viewport_origin_y=origin_y,
        scale_x=point_scale_x,
        scale_y=point_scale_y,
    )


def _require_display_size(width: int, height: int) -> None:
    if (
        isinstance(width, bool)
        or not isinstance(width, int)
        or isinstance(height, bool)
        or not isinstance(height, int)
        or width <= 0
        or height <= 0
    ):
        raise SagasuError(
            "coordinate_mapping_failed",
            "The X display dimensions are invalid",
            {"width": width, "height": height},
        )


def _require_finite(name: str, value: float) -> None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise SagasuError(
            "coordinate_mapping_failed",
            f"{name} is not numeric",
        )
    if not math.isfinite(value):
        raise SagasuError(
            "coordinate_mapping_failed",
            f"{name} is not finite",
        )


def _require_positive(name: str, value: float) -> None:
    _require_finite(name, value)
    if value <= 0:
        raise SagasuError(
            "coordinate_mapping_failed",
            f"{name} must be greater than zero",
            {name: value},
        )


def _nearest_pixel(value: float) -> int:
    return math.floor(value + 0.5)
