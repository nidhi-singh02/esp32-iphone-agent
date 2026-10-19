import asyncio

import pytest

from hidagent.phone import NUDGE, SHIFT_GUESS, Phone, fit_surface


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


def test_search_goes_home_twice_opens_spotlight_and_types(tmp_path, monkeypatch):
    async def no_sleep(_):
        pass

    monkeypatch.setattr("hidagent.phone.asyncio.sleep", no_sleep)
    phone, pointer = _phone(tmp_path, None)
    asyncio.run(phone.search("Reminders"))
    assert pointer.sent == ["key cmd+h", "key cmd+h", "key cmd+space", "type Reminders"]


def test_aim_corrects_the_tap_but_leaves_the_calibration_alone(tmp_path, monkeypatch):
    import json

    from hidagent import phone as phone_module

    (tmp_path / "cal.json").write_text(json.dumps({"w": 145, "h": 332, "seen": {"x": [[0.3, 0.3], [0.6, 0.6]], "y": []}}))
    pointer = Pointer(145, 332)

    class Screen:
        frame = b"x"

        async def settled(self, **kw):
            pass

        async def fresh(self, since, timeout=0.8):
            return self.frame

    phone = Phone(pointer, tmp_path / "cal.json", Screen())
    phone._pos = (0, 0)
    monkeypatch.setattr(phone_module, "STABLE_MAX", 0.05)
    monkeypatch.setattr(phone_module.vision, "pointer", lambda *a, **k: (0.55, 0.5))  # landed 0.05 right of the aim
    asyncio.run(phone._aim(0.5, 0.5))
    assert pointer.sent[-1].startswith("move -")  # this tap is corrected right away
    assert list(phone._seen["x"]) == [(0.3, 0.3), (0.6, 0.6)] and not phone._seen["y"]
    assert json.loads((tmp_path / "cal.json").read_text())["seen"]["x"] == [[0.3, 0.3], [0.6, 0.6]]


def landing(u, v):
    """A pointer whose x gain grows with the vertical distance and whose y drifts with the horizontal one."""
    return 0.05 + u / 145 * (1 + 0.00004 * v), 0.03 + v / 332 + 0.0002 * u


def grid(bad=None):
    pts = [(u, v, *landing(u, v)) for v in (30, 100, 170, 240, 310) for u in (20, 45, 70, 95, 120)]
    if bad:
        pts[bad] = (*pts[bad][:2], 0.2, 0.9)  # a misread pointer
    return pts


def test_fit_surface_recovers_the_landing_and_drops_a_misread_point():
    surface = fit_surface(grid(bad=12))
    assert surface["box"] == [20, 120, 30, 310]
    for u, v in ((20, 30), (70, 170), (120, 310), (33, 205)):
        x, y = landing(u, v)
        assert sum(c * t for c, t in zip(surface["x"], [1, u, v, u * v, u * u, v * v])) == pytest.approx(x, abs=0.002)
        assert sum(c * t for c, t in zip(surface["y"], [1, u, v, u * v, u * u, v * v])) == pytest.approx(y, abs=0.002)


def test_fit_surface_needs_enough_points():
    assert fit_surface(grid()[:8]) is None


def test_a_tap_inside_the_measured_area_lands_where_aimed_despite_the_cross_terms(tmp_path):
    cal = tmp_path / "cal.json"
    cal.write_text('{"w": 145, "h": 332, "seen": {"x": [], "y": []}}')
    phone = Phone(Pointer(145, 332), cal)
    phone._surface = fit_surface(grid())
    for x, y in ((0.4, 0.5), (0.8, 0.9), (0.3, 0.2)):
        u, v = phone._units(x, y)
        lx, ly = landing(u, v)
        assert (lx, ly) == pytest.approx((x, y), abs=0.008)  # one mouse unit is 0.007 of the width


def test_the_surface_is_saved_and_dropped_by_a_new_calibration(tmp_path):
    cal = tmp_path / "cal.json"
    cal.write_text('{"w": 145, "h": 332, "seen": {"x": [], "y": []}}')
    phone = Phone(Pointer(145, 332), cal)
    phone._surface = fit_surface(grid())
    phone._save()
    assert Phone(Pointer(145, 332), cal)._surface == phone._surface
    phone.finish_calibration(0.4, 0.4)
    assert Phone(Pointer(145, 332), cal)._surface is None


def test_aim_reads_the_pointer_from_its_ring_without_wiggling_it(tmp_path, monkeypatch):
    from hidagent import phone as phone_module

    (tmp_path / "cal.json").write_text('{"w": 145, "h": 332, "seen": {"x": [], "y": []}}')
    pointer = Pointer(145, 332)

    class Screen:
        frame = b"frame"

        async def settled(self, **kw):
            pass

    phone = Phone(pointer, tmp_path / "cal.json", Screen())
    phone._pos = (0, 0)
    monkeypatch.setattr(phone_module, "STABLE_AFTER", 0)
    monkeypatch.setattr(phone_module, "STABLE_POLL", 0.01)
    monkeypatch.setattr(phone_module.vision, "pointer_by_color", lambda frame: (0.55, 0.5))
    asyncio.run(phone._aim(0.5, 0.5))
    assert pointer.sent == ["move -7 0"]  # one correction, no nudge pair first

    pointer.sent.clear()
    monkeypatch.setattr(phone_module.vision, "pointer_by_color", lambda frame: (0.1, 0.9))  # a misread
    asyncio.run(phone._aim(0.5, 0.5))
    assert pointer.sent == []


def test_aim_waits_for_two_agreeing_readings_so_a_frame_mid_move_is_not_used(tmp_path, monkeypatch):
    from hidagent import phone as phone_module

    (tmp_path / "cal.json").write_text('{"w": 145, "h": 332, "seen": {"x": [], "y": []}}')
    pointer = Pointer(145, 332)

    class Screen:
        frame = b"frame"

    phone = Phone(pointer, tmp_path / "cal.json", Screen())
    phone._pos = (0, 0)
    monkeypatch.setattr(phone_module, "STABLE_AFTER", 0)
    monkeypatch.setattr(phone_module, "STABLE_POLL", 0.01)
    monkeypatch.setattr(phone_module, "STABLE_MAX", 0.3)
    readings = iter([(0.2, 0.3), (0.35, 0.45), (0.5, 0.5), (0.5, 0.5)])  # the picture catches up with the move
    monkeypatch.setattr(phone_module.vision, "pointer_by_color", lambda frame: next(readings, (0.5, 0.5)))
    assert asyncio.run(phone._read_pointer()) == (0.5, 0.5)

    drifting = iter((0.1 * i, 0.1 * i) for i in range(1000))  # never settles
    monkeypatch.setattr(phone_module.vision, "pointer_by_color", lambda frame: next(drifting))
    assert asyncio.run(phone._read_pointer()) is None
    asyncio.run(phone._aim(0.5, 0.5))
    assert pointer.sent == []  # no steady reading, so no correction from a frame in flight


def test_aim_does_not_trust_a_reading_taken_right_after_the_move(tmp_path, monkeypatch):
    import time

    from hidagent import phone as phone_module

    (tmp_path / "cal.json").write_text('{"w": 145, "h": 332, "seen": {"x": [], "y": []}}')

    class Screen:
        frame = b"frame"

    phone = Phone(Pointer(145, 332), tmp_path / "cal.json", Screen())
    monkeypatch.setattr(phone_module, "STABLE_POLL", 0.01)
    monkeypatch.setattr(phone_module, "STABLE_MAX", 0.2)
    monkeypatch.setattr(phone_module.vision, "pointer_by_color", lambda frame: (0.5, 0.5))
    phone._moved_at = time.time()  # the pointer has just moved: STABLE_AFTER (0.6 s) has not passed
    assert asyncio.run(phone._read_pointer()) is None
