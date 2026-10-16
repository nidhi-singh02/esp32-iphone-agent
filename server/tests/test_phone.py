import asyncio

import pytest

from hidagent.phone import NUDGE, SHIFT_GUESS, Phone


class Pointer:
    """A simulated phone pointer: a straight line from a small start offset, stuck at the screen edge."""

    def __init__(self, span_w, span_h, off=(0.045, 0.02)):
        self.span_w, self.span_h, self.off = span_w, span_h, off
        self.units = [0, 0]
        self.sent = []
        self.links = 1

    async def send(self, command):
        self.sent.append(command)
        name, *args = command.split()
        if name == "home":
            self.units = [0, 0]
        elif name == "move":
            self.units = [self.units[0] + int(args[0]), self.units[1] + int(args[1])]

    def position(self):
        return (min(self.off[0] + self.units[0] / self.span_w, 0.988),
                min(self.off[1] + self.units[1] / self.span_h, 0.988))


def calibrate(tmp_path, pointer):
    class Screen:
        live = True

    phone = Phone(pointer, tmp_path / "cal.json", Screen())

    async def find(shift=0.025):
        x, y = pointer.position()
        # what vision.pointer returns: the centroid of both pointer spots, minus half the shift it was told
        return x + (NUDGE / pointer.span_w - shift) / 2, y

    phone._find_pointer = find
    ok = asyncio.run(phone.start_calibration())
    return phone, ok


@pytest.mark.parametrize("span", [(136, 329), (202, 317), (480, 700)])
def test_calibration_finds_the_span_without_reaching_the_screen_edge(tmp_path, span):
    pointer = Pointer(*span)
    phone, ok = calibrate(tmp_path, pointer)
    assert ok
    assert phone.span["w"] == pytest.approx(span[0], rel=0.05)
    assert phone.span["h"] == pytest.approx(span[1], rel=0.05)
    moved = [int(c.split()[1]) for c in pointer.sent if c.startswith("move") and int(c.split()[1]) > NUDGE]
    assert moved and all(pointer.off[0] + sum(moved[:i + 1]) / span[0] < 0.9 for i in range(len(moved)))


def test_calibration_learns_the_start_offset(tmp_path):
    phone, ok = calibrate(tmp_path, Pointer(136, 329))
    assert ok
    # the pointer sits 4.5% from the left after home: a move aimed at 0.5 must be corrected to land there
    assert phone._fix(0.5, "x") == pytest.approx(0.455, abs=0.02)


def test_calibration_waits_for_a_click_when_the_pointer_is_never_found(tmp_path):
    pointer = Pointer(136, 329)

    class Screen:
        live = True

    phone = Phone(pointer, tmp_path / "cal.json", Screen())

    async def nothing(shift=SHIFT_GUESS):
        return None

    phone._find_pointer = nothing
    assert asyncio.run(phone.start_calibration()) is False
    assert pointer.sent[-1] == "move 60 60"  # the manual fallback stays well inside the screen


def test_every_tap_starts_from_the_homed_pointer(tmp_path):
    import json

    (tmp_path / "cal.json").write_text(json.dumps({"w": 136, "h": 329, "seen": {"x": [], "y": []}}))
    pointer = Pointer(136, 329)
    phone = Phone(pointer, tmp_path / "cal.json", None)

    async def run():
        await phone.tap(0.5, 0.8)
        await phone.tap(0.2, 0.3)
        await phone.tap(0.9, 0.1)

    asyncio.run(run())
    homes = [i for i, c in enumerate(pointer.sent) if c == "home"]
    clicks = [i for i, c in enumerate(pointer.sent) if c == "click"]
    assert len(homes) == 3 and all(h < c for h, c in zip(homes, clicks))  # a home before each of the three clicks


def _phone(tmp_path, screen):
    import json

    (tmp_path / "cal.json").write_text(json.dumps({"w": 136, "h": 329, "seen": {"x": [], "y": []}}))
    pointer = Pointer(136, 329)
    return Phone(pointer, tmp_path / "cal.json", screen), pointer


def _jpeg(color):
    import io

    from PIL import Image

    out = io.BytesIO()
    Image.new("RGB", (498, 1080), color).save(out, "JPEG")
    return out.getvalue()


class FakeScreen:
    def __init__(self, before, after):
        self.frame, self._after = before, after

    async def fresh(self, since, timeout=0.8):
        self.frame = self._after
        return self.frame

    async def settled(self, quiet=0.35, least=0.25, most=4):
        pass


def test_go_home_uses_cmd_h_and_stops_when_the_screen_changed(tmp_path):
    phone, pointer = _phone(tmp_path, FakeScreen(_jpeg("white"), _jpeg("black")))
    asyncio.run(phone.go_home())
    assert pointer.sent == ["key cmd+h"]


def test_go_home_swipes_from_the_bottom_edge_when_cmd_h_changed_nothing(tmp_path):
    same = _jpeg("white")
    phone, pointer = _phone(tmp_path, FakeScreen(same, same))
    asyncio.run(phone.go_home())
    assert pointer.sent[0] == "key cmd+h"
    assert "press" in pointer.sent and "release" in pointer.sent and "home" in pointer.sent
    first_move = next(c for c in pointer.sent if c.startswith("move"))
    assert int(first_move.split()[2]) >= 320  # the pointer goes to the bottom edge (329 units is the full height)


def test_go_home_without_a_screen_only_sends_cmd_h(tmp_path):
    phone, pointer = _phone(tmp_path, None)
    asyncio.run(phone.go_home())
    assert pointer.sent == ["key cmd+h"]
