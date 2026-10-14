import asyncio
import subprocess
import sys
import time

import pytest
from PIL import Image

from hidagent.macos_airplay import (
    HIDDEN_HELP,
    INSTALL_HELP,
    MAC_ONLY,
    PERMISSION_HELP,
    WINDOW_HELP,
    MacAirPlayScreen,
    crop_letterbox,
    pick_airplay_window,
)
from hidagent.screen import Screen, open_screen


def _window(owner="AirPlayUIAgent", title="iPhone", width=400, height=800, onscreen=True, number=7, alpha=1):
    return {
        "kCGWindowOwnerName": owner,
        "kCGWindowName": title,
        "kCGWindowNumber": number,
        "kCGWindowBounds": {"X": 0, "Y": 0, "Width": width, "Height": height},
        "kCGWindowIsOnscreen": onscreen,
        "kCGWindowAlpha": alpha,
    }


def _picture(size, box=None, color=(20, 120, 200), bg=(0, 0, 0)):
    image = Image.new("RGB", size, bg)
    if box:
        image.paste(Image.new("RGB", (box[2] - box[0], box[3] - box[1]), color), (box[0], box[1]))
    return image


def _wait(ready, timeout=2.0):
    end = time.time() + timeout
    while time.time() < end:
        if ready():
            return True
        time.sleep(0.02)
    return False


def test_aspect_reads_the_jpeg_header():
    screen = Screen()
    screen.frame = b"\xff\xd8\xff\xc0\x00\x11\x08" + (200).to_bytes(2, "big") + (90).to_bytes(2, "big") + b"\xff\xd9"
    assert screen.aspect == pytest.approx(90 / 200)
    screen.frame = None
    assert screen.aspect == pytest.approx(9 / 19.5)


def test_default_source_is_uxplay(monkeypatch):
    monkeypatch.delenv("SCREEN_SOURCE", raising=False)
    monkeypatch.setenv("AIRPLAY_NAME", "Desk")
    screen = open_screen()
    assert isinstance(screen, Screen)
    assert screen.name == "Desk"


def test_macos_airplay_is_refused_off_mac(monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setenv("SCREEN_SOURCE", "macos_airplay")
    with pytest.raises(RuntimeError, match="only works on macOS"):
        open_screen()


def test_macos_airplay_reads_window_overrides(monkeypatch):
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setenv("SCREEN_SOURCE", "macos-airplay")
    monkeypatch.setenv("AIRPLAY_WINDOW_OWNER", "CustomAgent")
    monkeypatch.setenv("AIRPLAY_WINDOW_TITLE", "Jane's iPhone")
    screen = open_screen()
    assert isinstance(screen, MacAirPlayScreen)
    assert screen.owner == "CustomAgent" and screen.title == "Jane's iPhone"


def test_unknown_screen_source(monkeypatch):
    monkeypatch.setenv("SCREEN_SOURCE", "quicktime")
    with pytest.raises(RuntimeError, match="macos_airplay"):
        open_screen()


def test_pick_default_owner_is_case_insensitive_and_skips_other_apps():
    chosen, err = pick_airplay_window([
        _window(owner="Safari", title="AirPlay help", width=1400, height=900, number=1),
        _window(owner="airplayuiagent", title="", width=300, height=640, number=2),
        _window(owner="AirPlayUIAgent", title="iPhone", width=500, height=900, number=3),
    ])
    assert err == "" and chosen["id"] == 3


def test_pick_owner_or_title_override():
    windows = [_window(owner="ControlCenter", title="Jane's iPhone", number=4)]
    assert pick_airplay_window(windows)[0] is None
    chosen, err = pick_airplay_window(windows, owner="controlcenter")
    assert err == "" and chosen["id"] == 4
    chosen, err = pick_airplay_window(windows, title="jane's")
    assert err == "" and chosen["id"] == 4
    assert pick_airplay_window(windows, owner="AirPlayUIAgent, ControlCenter", title="ipad")[0] is None
    chosen, err = pick_airplay_window(windows, owner="Nope, ControlCenter", title="iPhone")
    assert err == "" and chosen["id"] == 4


def test_pick_ignores_tiny_or_invisible_windows_and_names_a_hidden_one():
    assert pick_airplay_window([_window(width=40, height=40)])[1] == WINDOW_HELP
    assert pick_airplay_window([_window(alpha=0)])[1] == WINDOW_HELP
    assert pick_airplay_window([_window(onscreen=False)])[1] == HIDDEN_HELP


def test_crop_letterbox_keeps_one_axis_bars_only():
    pillar = _picture((200, 100), (80, 0, 120, 100))  # black on the left and right
    assert crop_letterbox(pillar).size == (40, 100)
    letter = _picture((100, 200), (0, 40, 100, 160))
    assert crop_letterbox(letter).size == (100, 120)
    inset = _picture((200, 200), (30, 30, 170, 170))  # black on all four sides: a dark screen
    assert crop_letterbox(inset).size == (200, 200)
    full = _picture((90, 190), color=(8, 8, 8), bg=(8, 8, 8))
    assert crop_letterbox(full).size == (90, 190)


def test_crop_letterbox_treats_near_black_as_a_bar_and_ignores_a_hairline():
    near = _picture((180, 100), (30, 0, 150, 100), bg=(10, 10, 10))
    assert crop_letterbox(near).size == (120, 100)
    gray = _picture((180, 100), (30, 0, 150, 100), bg=(40, 40, 40))
    assert crop_letterbox(gray).size == (180, 100)
    hairline = _picture((100, 80), (2, 0, 98, 80))
    assert crop_letterbox(hairline).size == (100, 80)
    # a hairline on the other axis must not cancel real side bars
    mixed = _picture((200, 120), (40, 3, 160, 120))
    assert crop_letterbox(mixed).size == (120, 117)
    tiny = _picture((100, 40), (40, 0, 60, 40))  # side bars, but the content is under 32px wide
    assert crop_letterbox(tiny).size == (100, 40)


class _Started:
    def __init__(self, screen):
        self.screen = screen

    def __enter__(self):
        self.screen.start()
        return self.screen

    def __exit__(self, *_exc):
        self.screen.stop()


def _screen(grab, windows=None, fps=40, **kw):
    return MacAirPlayScreen(fps=fps, windows=windows or (lambda: [_window()]), grab=grab,
                            recording_allowed=lambda: True, **kw)


def test_capture_publishes_a_cropped_jpeg_and_does_not_launch_uxplay(monkeypatch):
    def refuse(*args, **kwargs):
        raise AssertionError(f"Popen called: {args}")

    monkeypatch.setattr(subprocess, "Popen", refuse)
    screen = _screen(lambda _wid: _picture((200, 100), (80, 0, 120, 100), color=(240, 10, 10)))
    with _Started(screen):
        assert _wait(lambda: screen.frame is not None)
    assert screen.frame.startswith(b"\xff\xd8") and screen.frame.endswith(b"\xff\xd9")
    assert screen.aspect == pytest.approx(40 / 100, rel=0.02)
    assert screen.error is None


def test_static_frames_do_not_look_new_and_a_change_does():
    state = {"image": _picture((90, 190), color=(10, 20, 30), bg=(10, 20, 30))}
    screen = _screen(lambda _wid: state["image"])
    with _Started(screen):
        assert _wait(lambda: screen.frame is not None)
        stamped = screen.updated
        assert asyncio.run(screen.fresh(stamped, timeout=0.15)) is None
        started = time.time()
        asyncio.run(screen.settled(quiet=0.2, least=0.05, most=2))
        assert time.time() - started < 1
        assert screen.live
        state["image"] = _picture((90, 190), color=(200, 30, 30), bg=(200, 30, 30))
        assert asyncio.run(screen.fresh(stamped, timeout=1)) is not None


def test_missing_permission_and_missing_window_are_explicit(monkeypatch):
    def refuse(*args, **kwargs):
        raise AssertionError("uxplay launched")

    monkeypatch.setattr(subprocess, "Popen", refuse)
    denied = MacAirPlayScreen(windows=lambda: [_window()], grab=lambda _wid: _picture((90, 190)),
                              recording_allowed=lambda: False)
    with _Started(denied):
        assert _wait(lambda: denied.error == PERMISSION_HELP)
    assert denied.frame is None
    assert "Terminal" in PERMISSION_HELP and "Screen Recording" in PERMISSION_HELP

    missing = _screen(lambda _wid: _picture((90, 190)), windows=lambda: [])
    with _Started(missing):
        assert _wait(lambda: missing.error == WINDOW_HELP)
    assert "AIRPLAY_WINDOW_OWNER" in WINDOW_HELP

    hidden = _screen(lambda _wid: _picture((90, 190)), windows=lambda: [_window(onscreen=False)])
    with _Started(hidden):
        assert _wait(lambda: hidden.error == HIDDEN_HELP)


def test_linux_and_missing_quartz_fail_before_capture(monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    screen = MacAirPlayScreen()
    with _Started(screen):
        assert _wait(lambda: screen.error == MAC_ONLY)

    monkeypatch.setattr(sys, "platform", "darwin")

    def missing_quartz():
        raise ImportError("Quartz")

    monkeypatch.setattr("hidagent.macos_airplay.load_quartz", missing_quartz)
    bare = MacAirPlayScreen()
    with _Started(bare):
        assert _wait(lambda: bare.error == INSTALL_HELP)
    assert "[mac]" in INSTALL_HELP
