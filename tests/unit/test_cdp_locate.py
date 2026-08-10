from __future__ import annotations

import pytest

from sagasu.cdp import locate
from sagasu.cdp.client import PageTarget
from sagasu.protocol import SagasuError


TARGET = PageTarget(
    target_id="target-1",
    title="Google",
    url="https://www.google.com/",
    websocket_url="ws://127.0.0.1:9222/devtools/page/target-1",
)


class FakeSession:
    def __init__(self, responses):
        self.responses = responses
        self.calls = []
        self.closed = False

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.closed = True

    def call(self, method, params=None):
        self.calls.append((method, params or {}))
        response = self.responses[method]
        if isinstance(response, Exception):
            raise response
        return response


class FakeClient:
    def __init__(self, responses):
        self.session = FakeSession(responses)
        self.opened = []

    def page_targets(self):
        return [TARGET]

    def open(self, target):
        self.opened.append(target)
        return self.session


def responses(*, node_id=73, quads=None):
    return {
        "DOM.getDocument": {"root": {"nodeId": 1}},
        "DOM.querySelector": {"nodeId": node_id},
        "DOM.getContentQuads": {
            "quads": quads
            or [
                [
                    379.5,
                    222,
                    825.859375,
                    222,
                    825.859375,
                    272,
                    379.5,
                    272,
                ]
            ]
        },
        "Page.getLayoutMetrics": {
            "cssVisualViewport": {
                "clientWidth": 1351,
                "clientHeight": 695,
                "zoom": 1,
            }
        },
        "Browser.getWindowForTarget": {
            "windowId": 1,
            "bounds": {
                "left": 0,
                "top": 0,
                "width": 1365,
                "height": 767,
                "windowState": "normal",
            },
        },
        "Runtime.evaluate": {
            "result": {
                "type": "object",
                "value": {
                    "screenWidth": 1366,
                    "screenHeight": 768,
                    "innerHeight": 695,
                    "outerHeight": 767,
                },
            }
        },
    }


def test_locates_element_in_absolute_x_display_coordinates():
    client = FakeClient(responses())

    result = locate.locate_active_element(
        "textarea[name=q]",
        display_width=1366,
        display_height=768,
        client=client,
    )

    assert client.opened == [TARGET]
    assert client.session.closed is True
    assert client.session.calls == [
        ("DOM.getDocument", {"depth": 0, "pierce": False}),
        (
            "DOM.querySelector",
            {"nodeId": 1, "selector": "textarea[name=q]"},
        ),
        ("Page.getLayoutMetrics", {}),
        ("DOM.getContentQuads", {"nodeId": 73}),
        (
            "Browser.getWindowForTarget",
            {"targetId": "target-1"},
        ),
        (
            "Runtime.evaluate",
            {
                "expression": (
                    "({screenWidth:window.screen.width,"
                    "screenHeight:window.screen.height,"
                    "innerHeight:window.innerHeight,"
                    "outerHeight:window.outerHeight})"
                ),
                "returnByValue": True,
            },
        ),
    ]
    assert (result.screen_x, result.screen_y) == (603, 319)
    assert (result.viewport_x, result.viewport_y) == pytest.approx(
        (602.6796875, 247)
    )
    assert result.viewport_origin_y == 72
    assert result.selector == "textarea[name=q]"
    assert result.target_id == "target-1"


def test_horizontal_scrollbar_does_not_shift_screen_coordinates():
    scrollbar = responses()
    scrollbar["Page.getLayoutMetrics"]["cssVisualViewport"][
        "clientHeight"
    ] = 680

    result = locate.locate_active_element(
        "textarea[name=q]",
        display_width=1366,
        display_height=768,
        client=FakeClient(scrollbar),
    )

    assert result.viewport_origin_y == 72
    assert (result.screen_x, result.screen_y) == (603, 319)


def test_missing_viewport_zoom_defaults_to_unzoomed():
    without_zoom = responses()
    del without_zoom["Page.getLayoutMetrics"]["cssVisualViewport"]["zoom"]

    result = locate.locate_active_element(
        "textarea[name=q]",
        display_width=1366,
        display_height=768,
        client=FakeClient(without_zoom),
    )

    assert result.scale_x == 1
    assert result.scale_y == 1
    assert (result.screen_x, result.screen_y) == (603, 319)


@pytest.mark.parametrize(
    "zoom",
    [None, True, "1", 0, -1, float("inf"), float("nan")],
)
def test_rejects_malformed_or_non_positive_supplied_viewport_zoom(zoom):
    malformed = responses()
    malformed["Page.getLayoutMetrics"]["cssVisualViewport"]["zoom"] = zoom

    with pytest.raises(SagasuError) as error:
        locate.locate_active_element(
            "textarea[name=q]",
            display_width=1366,
            display_height=768,
            client=FakeClient(malformed),
        )

    assert error.value.code == "invalid_response"


def test_uses_center_of_the_visible_part_of_a_clipped_quad():
    client = FakeClient(
        responses(
            quads=[
                [-20, 100, 20, 100, 20, 140, -20, 140],
            ]
        )
    )

    result = locate.locate_active_element(
        "#partially-visible",
        display_width=1366,
        display_height=768,
        client=client,
    )

    assert (result.viewport_x, result.viewport_y) == pytest.approx((10, 120))
    assert (result.screen_x, result.screen_y) == (10, 192)
    assert result.visible_polygon == (
        (0, 100),
        (20.0, 100.0),
        (20.0, 140.0),
        (0, 140),
    )


def test_missing_or_offscreen_elements_are_structured_errors():
    with pytest.raises(SagasuError) as missing:
        locate.locate_active_element(
            "#missing",
            display_width=1366,
            display_height=768,
            client=FakeClient(responses(node_id=0)),
        )
    assert missing.value.code == "element_not_found"

    with pytest.raises(SagasuError) as offscreen:
        locate.locate_active_element(
            "#below-fold",
            display_width=1366,
            display_height=768,
            client=FakeClient(
                responses(
                    quads=[
                        [10, 800, 20, 800, 20, 820, 10, 820],
                    ]
                )
            ),
        )
    assert offscreen.value.code == "element_not_visible"


def test_selector_validation_and_malformed_metrics_fail_safely(monkeypatch):
    with pytest.raises(SagasuError) as empty:
        locate.locate_active_element(
            "",
            display_width=1366,
            display_height=768,
            client=object(),
        )
    assert empty.value.code == "invalid_arguments"

    monkeypatch.setattr(locate, "MAX_SELECTOR_BYTES", 3)
    with pytest.raises(SagasuError) as oversized:
        locate.locate_active_element(
            "文字",
            display_width=1366,
            display_height=768,
            client=object(),
        )
    assert oversized.value.code == "invalid_arguments"

    malformed = responses()
    malformed["Page.getLayoutMetrics"] = {"cssVisualViewport": {}}
    with pytest.raises(SagasuError) as metrics:
        locate.locate_active_element(
            "#x",
            display_width=1366,
            display_height=768,
            client=FakeClient(malformed),
        )
    assert metrics.value.code == "invalid_response"
