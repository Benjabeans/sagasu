"""Opt-in checks against a built, running Sagasu session container.

Run with:
    SAGASU_LIVE_CONTAINER=sagasu-preview pytest -m live tests/integration
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest


pytestmark = pytest.mark.live
CONTAINER = os.environ.get("SAGASU_LIVE_CONTAINER")
SECOND_CONTAINER = os.environ.get("SAGASU_LIVE_CONTAINER_2")
REPOSITORY_ROOT = Path(__file__).resolve().parents[2]


def executor(
    *arguments: str,
    binary: bool = False,
    container: str | None = CONTAINER,
):
    if not container:
        pytest.skip("SAGASU_LIVE_CONTAINER is not set")
    return subprocess.run(
        [
            "docker",
            "exec",
            "--user",
            "sagasu",
            container,
            "sagasu-session-exec",
            *arguments,
        ],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
        text=not binary,
    )


def test_live_display_move_and_screenshot():
    display = executor("display")
    assert display.returncode == 0, display.stderr
    dimensions = json.loads(display.stdout)["display"]

    move = executor(
        "cursor",
        "move",
        str(dimensions["width"] // 2),
        str(dimensions["height"] // 2),
        "--duration-ms",
        "50",
    )
    assert move.returncode == 0, move.stderr
    assert json.loads(move.stdout)["backend"] == "humancursor"

    screenshot = executor("screenshot", binary=True)
    assert screenshot.returncode == 0, screenshot.stderr
    assert screenshot.stdout.startswith(b"\x89PNG\r\n\x1a\n")


def test_live_legacy_xcontrol_alias_remains_available():
    if not CONTAINER:
        pytest.skip("SAGASU_LIVE_CONTAINER is not set")
    result = subprocess.run(
        [
            "docker",
            "exec",
            "--user",
            "sagasu",
            CONTAINER,
            "sagasu-xcontrol",
            "display",
        ],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["operation"] == "display"


def test_live_dom_stream_and_host_publication(tmp_path):
    streamed = executor("dom", binary=True)
    assert streamed.returncode == 0, streamed.stderr
    assert b"<html" in streamed.stdout.lower()
    metadata = json.loads(streamed.stderr.decode().splitlines()[-1])
    assert metadata["operation"] == "dom.fetch"
    assert metadata["backend"] == "cdp"
    assert metadata["bytes"] == len(streamed.stdout)

    output = tmp_path / "page.html"
    host = subprocess.run(
        [
            sys.executable,
            "-m",
            "sagasu.cli.main",
            "session",
            "dom",
            "--container",
            CONTAINER,
            "--out",
            str(output),
        ],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
        text=True,
        cwd=REPOSITORY_ROOT,
        env={
            **os.environ,
            "PYTHONPATH": str(REPOSITORY_ROOT / "src"),
        },
    )
    assert host.returncode == 0, host.stderr
    payload = json.loads(host.stdout)
    assert payload["operation"] == "dom.fetch"
    assert payload["output"] == str(output)
    assert payload["bytes"] == output.stat().st_size
    assert "<html" in output.read_text().lower()


def test_live_locate_returns_a_point_inside_the_x_display():
    located = executor("locate", "html")
    assert located.returncode == 0, located.stderr
    payload = json.loads(located.stdout)
    assert payload["operation"] == "element.locate"
    assert payload["backend"] == "cdp"
    assert 0 <= payload["screen"]["x"] < payload["display"]["width"]
    assert 0 <= payload["screen"]["y"] < payload["display"]["height"]


def test_live_explicit_xdotool_fallback():
    result = executor(
        "cursor", "move", "5", "5", "--backend", "xdotool"
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["backend"] == "xdotool"


def test_live_click_drag_scroll_pause_and_host_screenshot(tmp_path):
    click = executor(
        "cursor", "click", "300", "300", "--hold-ms", "10"
    )
    assert click.returncode == 0, click.stderr
    assert json.loads(click.stdout)["operation"] == "cursor.click"

    drag = executor(
        "cursor",
        "drag",
        "300",
        "300",
        "320",
        "300",
        "--duration-ms",
        "100",
        "--steady",
    )
    assert drag.returncode == 0, drag.stderr
    assert json.loads(drag.stdout)["pointer"] == {"x": 320, "y": 300}

    scroll = executor("cursor", "scroll", "320", "300", "--steps", "-2")
    assert scroll.returncode == 0, scroll.stderr
    assert json.loads(scroll.stdout)["operation"] == "cursor.scroll"

    paused = executor("human", "pause")
    assert paused.returncode == 0, paused.stderr
    rejected = executor("cursor", "move", "10", "10")
    assert rejected.returncode != 0
    assert json.loads(rejected.stderr)["error"]["code"] == "human_control"
    observed = executor("display")
    assert observed.returncode == 0, observed.stderr
    resumed = executor("human", "resume")
    assert resumed.returncode == 0, resumed.stderr

    output = tmp_path / "host-screen.png"
    host = subprocess.run(
        [
            sys.executable,
            "-m",
            "sagasu.cli.main",
            "session",
            "screenshot",
            "--container",
            CONTAINER,
            "--out",
            str(output),
            "--no-pointer",
        ],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
        text=True,
        cwd=REPOSITORY_ROOT,
        env={
            **os.environ,
            "PYTHONPATH": str(REPOSITORY_ROOT / "src"),
        },
    )
    assert host.returncode == 0, host.stderr
    assert json.loads(host.stdout)["pointer_included"] is False
    assert output.read_bytes().startswith(b"\x89PNG\r\n\x1a\n")


def test_live_same_session_contention_is_nonblocking():
    if not CONTAINER:
        pytest.skip("SAGASU_LIVE_CONTAINER is not set")
    long_move = subprocess.Popen(
        [
            "docker",
            "exec",
            "--user",
            "sagasu",
            CONTAINER,
            "sagasu-session-exec",
            "cursor",
            "move",
            "900",
            "500",
            "--duration-ms",
            "1500",
        ],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        time.sleep(0.2)
        started = time.monotonic()
        contended = executor("cursor", "move", "100", "100")
        elapsed = time.monotonic() - started
        assert contended.returncode != 0
        assert json.loads(contended.stderr)["error"]["code"] == "session_busy"
        assert elapsed < 0.5
        if SECOND_CONTAINER:
            other_started = time.monotonic()
            other = executor("display", container=SECOND_CONTAINER)
            assert other.returncode == 0, other.stderr
            assert time.monotonic() - other_started < 0.5
    finally:
        stdout, stderr = long_move.communicate(timeout=5)
    assert long_move.returncode == 0, stderr
    assert json.loads(stdout)["operation"] == "cursor.move"


def test_live_same_display_number_is_container_isolated():
    if not SECOND_CONTAINER:
        pytest.skip("SAGASU_LIVE_CONTAINER_2 is not set")
    first = executor(
        "cursor",
        "move",
        "111",
        "112",
        "--backend",
        "xdotool",
    )
    second = executor(
        "cursor",
        "move",
        "211",
        "212",
        "--backend",
        "xdotool",
        container=SECOND_CONTAINER,
    )
    assert first.returncode == second.returncode == 0
    first_position = json.loads(executor("cursor", "position").stdout)
    second_position = json.loads(
        executor("cursor", "position", container=SECOND_CONTAINER).stdout
    )
    assert first_position["pointer"] == {"x": 111, "y": 112}
    assert second_position["pointer"] == {"x": 211, "y": 212}
    first_png = executor("screenshot", binary=True).stdout
    second_png = executor(
        "screenshot", binary=True, container=SECOND_CONTAINER
    ).stdout
    assert first_png.startswith(b"\x89PNG\r\n\x1a\n")
    assert second_png.startswith(b"\x89PNG\r\n\x1a\n")
    assert first_png != second_png
