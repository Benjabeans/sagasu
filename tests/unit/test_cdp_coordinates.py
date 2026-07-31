from __future__ import annotations

import pytest

from sagasu.cdp.coordinates import (
    BrowserWindowBounds,
    CSSScreenSize,
    ViewportMetrics,
    convert_viewport_to_screen,
)
from sagasu.protocol import SagasuError


def test_converts_live_container_viewport_point_to_x_display():
    converted = convert_viewport_to_screen(
        602.6796875,
        247,
        window=BrowserWindowBounds(
            left=0,
            top=0,
            width=1365,
            height=767,
        ),
        viewport=ViewportMetrics(
            width=1351,
            height=695,
            zoom=1,
        ),
        css_screen=CSSScreenSize(width=1366, height=768),
        display_width=1366,
        display_height=768,
    )

    assert (converted.x, converted.y) == (603, 319)
    assert converted.viewport_origin_x == 0
    assert converted.viewport_origin_y == 72
    assert converted.scale_x == 1
    assert converted.scale_y == 1


def test_accounts_for_page_zoom_and_x_display_scaling():
    converted = convert_viewport_to_screen(
        100,
        50,
        window=BrowserWindowBounds(
            left=10,
            top=20,
            width=680,
            height=500,
        ),
        viewport=ViewportMetrics(
            width=300,
            height=200,
            zoom=2,
        ),
        css_screen=CSSScreenSize(width=1000, height=600),
        display_width=2000,
        display_height=1200,
    )

    assert (converted.x, converted.y) == (420, 440)
    assert converted.viewport_origin_x == 20
    assert converted.viewport_origin_y == 240
    assert converted.scale_x == 4
    assert converted.scale_y == 4


def test_rejects_points_or_metrics_that_cannot_map_to_the_display():
    window = BrowserWindowBounds(left=0, top=0, width=100, height=100)
    screen = CSSScreenSize(width=100, height=100)

    with pytest.raises(SagasuError) as outside:
        convert_viewport_to_screen(
            80,
            10,
            window=window,
            viewport=ViewportMetrics(width=50, height=50, zoom=1),
            css_screen=screen,
            display_width=100,
            display_height=100,
        )
    assert outside.value.code == "coordinate_mapping_failed"

    with pytest.raises(SagasuError) as oversized:
        convert_viewport_to_screen(
            10,
            10,
            window=window,
            viewport=ViewportMetrics(width=60, height=60, zoom=2),
            css_screen=screen,
            display_width=100,
            display_height=100,
        )
    assert oversized.value.code == "coordinate_mapping_failed"
