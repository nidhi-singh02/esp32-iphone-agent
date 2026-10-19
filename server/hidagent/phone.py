"""Touch-like gestures on the phone, built from relative mouse moves.

The pointer is relative, so the server tracks where it is (in mouse units) and moves
from there; every few gestures it slams it into the top-left corner (`home`) again to
cancel drift. Coordinates are fractions of the screen (0..1, origin top-left), so they
work for any frame size.
"""

import asyncio
import json
import logging
import time
from collections import deque
from pathlib import Path

from . import vision
from .device import Device

log = logging.getLogger(__name__)

CALIBRATION_MOVE = 60   # mouse units moved diagonally for a manual (one point) calibration; stays on screen
CALIBRATION_PROBES = (40, 25, 70)  # first stop of the automatic calibration: the first distance whose pointer is found
CALIBRATION_REACH = 0.75  # the second stop goes to this fraction of the smaller span, so no axis hits the screen edge
EDGE = 0.9              # a pointer found beyond this fraction of the screen may be stuck at the edge: not trusted
SHIFT_GUESS = 0.04      # screen fraction a NUDGE is assumed to move the pointer before the span is known
REHOME_EVERY = 1        # gestures between re-homing: every one, so a move always runs the long path the calibration measured
HOLD = 0.25             # seconds to rest before lifting, so iOS adds no momentum (the swipe stops where it ends)
TAP_GAP = 0.15          # seconds to stay still after a click, so iOS sees a finished tap, not the start of a drag
NUDGE = 12              # mouse units the pointer is wiggled by to find it on screen
AIM_TOLERANCE = 0.008   # screen fraction; a bigger miss is corrected before pressing
LEARN_FROM_AIM = False  # fold what aim sees into the calibration line: off, one misread pointer bends the line
OUTLIER = 0.04          # a grid reading this far off the fitted surface is a misread pointer, not iOS
STABLE_AFTER = 0.6      # seconds after a move before its pointer reading is trusted: AirPlay shows it late
STABLE_POLL = 0.15      # seconds between the two readings that must agree
STABLE_MAX = 2.0        # give up on a steady reading after this long and tap where the pointer is
MAX_JUMP = 0.25         # a reading this far from the aim is a misread (a frame caught mid-move), not corrected
MAX_MISS = 0.06         # bigger misses are still corrected, but not learned from (they may be misreadings)


def _solve(rows: list[list[float]], targets: list[float]) -> list[float]:
    """Least squares by the normal equations (Gauss-Jordan); no numpy needed for six unknowns."""
    n = len(rows[0])
    m = [[sum(r[i] * r[j] for r in rows) for j in range(n)] + [sum(r[i] * t for r, t in zip(rows, targets))]
         for i in range(n)]
    for i in range(n):
        p = max(range(i, n), key=lambda k: abs(m[k][i]))
        m[i], m[p] = m[p], m[i]
        for k in range(n):
            if k != i:
                f = m[k][i] / m[i][i]
                m[k] = [a - f * b for a, b in zip(m[k], m[i])]
    return [m[i][n] / m[i][i] for i in range(n)]


def _terms(u: float, v: float) -> list[float]:
    return [1, u, v, u * v, u * u, v * v]


def fit_surface(samples: list[tuple[float, float, float, float]]) -> dict | None:
    """Where the pointer lands for a move of (u, v) mouse units from the corner, as a quadratic per axis.

    samples: (u, v, x, y) landings seen on screen. iOS pointer acceleration makes the landing on one axis depend
    on the distance travelled on the other, which a line per axis cannot capture. Points that miss the fit by
    more than OUTLIER are dropped (a misread pointer) and the rest refitted. None when too few points remain.
    """
    pts = list(samples)
    for _ in range(3):
        if len(pts) < 12:
            return None
        rows = [_terms(u, v) for u, v, _, _ in pts]
        fits = [_solve(rows, [p[2 + a] for p in pts]) for a in range(2)]
        err = [max(abs(sum(c * t for c, t in zip(fits[a], r)) - p[2 + a]) for a in range(2)) for r, p in zip(rows, pts)]
        keep = [p for p, e in zip(pts, err) if e <= OUTLIER]
        if len(keep) == len(pts):
            break
        pts = keep
    else:
        return None
    return {"x": fits[0], "y": fits[1], "box": [min(p[0] for p in pts), max(p[0] for p in pts),
                                                 min(p[1] for p in pts), max(p[1] for p in pts)]}


class Phone:
    def __init__(self, device: Device, calibration_file: Path, screen=None):
        self.device, self._file, self.screen = device, calibration_file, screen
        # mouse units needed to cross the full screen width / height
        saved = json.loads(calibration_file.read_text()) if calibration_file.exists() else None
        self.span = {"w": saved["w"], "h": saved["h"]} if saved else None
        self._pos = None  # pointer position in mouse units; None = unknown
        self._moves = 0
        self._moved_at = 0.0  # when the pointer last moved
        self._ringed = False  # the last aim saw an orange ring on screen
        self._link = 0  # device.links when the pointer was last homed
        # where the pointer really landed (seen on screen) for the screen fraction we aimed at, per axis;
        # a straight-line fit through them corrects every later move, so taps get better as the agent works
        self._surface = (saved or {}).get("surface")  # measured landings over the screen (fit_surface), or None
        self._seen = {axis: deque(map(tuple, (saved or {}).get("seen", {}).get(axis, [])), maxlen=12)
                      for axis in ("x", "y")}

    @property
    def calibrated(self) -> bool:
        return self.span is not None

    async def start_calibration(self) -> bool:
        """Find the pointer on screen at two known distances from the corner and fit the mapping from both.

        Two points give the scale and the offset, which one point cannot (iOS pointer motion is not exactly
        proportional). The stops are chosen from what the first one shows, so that neither axis reaches the
        screen edge: a pointer stuck at the edge reads as a much larger span than the real one. Returns True
        when it calibrated itself; otherwise the user clicks the pointer (finish_calibration).
        """
        self._pos = None
        live = self.screen is not None and self.screen.live
        seen = []
        for first in CALIBRATION_PROBES if live else ():
            await self.device.send("home")
            await self.device.send(f"move {first} {first}")
            found = await self._find_pointer(SHIFT_GUESS)
            if found and found[0] < EDGE and found[1] < EDGE:
                seen.append((first, *found))
                break
        if seen:
            u1, x1, y1 = seen[0]
            # the smaller span is the nearer edge: aim the second stop well inside it
            u2 = max(round(CALIBRATION_REACH * min(u1 / x1, u1 / y1)), round(u1 * 1.4))
            await self.device.send(f"move {u2 - u1} {u2 - u1}")
            found = await self._find_pointer(SHIFT_GUESS)
            if found and found[0] < EDGE and found[1] < EDGE and found[0] - x1 > 0.08 and found[1] - y1 > 0.03:
                seen.append((u2, *found))
        if len(seen) < 2:
            log.info("calibration: pointer not found on screen, waiting for a click")
            await self.device.send("home")
            await self.device.send(f"move {CALIBRATION_MOVE} {CALIBRATION_MOVE}")
            return False
        (u1, x1, y1), (u2, x2, y2) = seen
        self.span = {"w": round((u2 - u1) / (x2 - x1)), "h": round((u2 - u1) / (y2 - y1))}
        # the nudge moved the pointer by NUDGE / span, not the guessed fraction: that shifts every x reading
        # by half the difference (a common shift, so it did not change the span)
        bias = (SHIFT_GUESS - NUDGE / self.span["w"]) / 2
        seen = [(u, x + bias, y) for u, x, y in seen]
        self._surface = None  # measured for the old span
        for samples in self._seen.values():
            samples.clear()
        for units, x, y in seen:  # where each stop landed, so moves are corrected for the offset too
            self._seen["x"].append((units / self.span["w"], x))
            self._seen["y"].append((units / self.span["h"], y))
        self._save()
        log.info("calibration: pointer at %s, span %s", [(round(x, 3), round(y, 3)) for _, x, y in seen], self.span)
        return True

    async def _find_pointer(self, shift: float = 0.025) -> tuple[float, float] | None:
        """Wiggle the pointer and find it anywhere on screen (the span may still be unknown).

        shift: the screen fraction the wiggle moves the pointer by, as far as it is known.
        """
        await self.screen.settled(quiet=0.2, least=0.3, most=1.5)
        before, sent = self.screen.frame, time.time()
        await self.device.send(f"move {NUDGE} 0")
        after = await self.screen.fresh(sent, 1.0)
        if after:
            await self.screen.settled(quiet=0.15, least=0.1, most=0.6)
            after = self.screen.frame
        await self.device.send(f"move -{NUDGE} 0")
        found = before and after and vision.pointer(before, after, (0.5, 0.5), shift, reach=(0.5, 0.5))
        return found if found and 0.03 < found[0] < 0.97 and 0.03 < found[1] < 0.97 else None

    def finish_calibration(self, x: float, y: float) -> None:
        """x, y: where the pointer ended up after start_calibration() (one point, picked by the user)."""
        self.span = {"w": round(CALIBRATION_MOVE / x), "h": round(CALIBRATION_MOVE / y)}
        self._surface = None
        for samples in self._seen.values():
            samples.clear()
        self._save()

    def _save(self) -> None:
        """Calibration plus the observed landings, so the correction survives a restart."""
        saved = {**self.span, "seen": {k: list(v) for k, v in self._seen.items()}}
        if self._surface:
            saved["surface"] = self._surface
        self._file.write_text(json.dumps(saved))

    async def point(self, x: float, y: float, aim: bool = False) -> None:
        if not self.calibrated:
            raise RuntimeError("pointer not calibrated")
        await self._move_to(*self._units(x, y))
        if aim and self.screen is not None:
            await self._aim(x, y)

    async def _aim(self, x: float, y: float) -> None:
        """Closed loop: find the real pointer on screen, fix a miss, and learn the error for later moves."""
        found = await self._read_pointer()  # the orange pointer ring: no wiggle
        if found is None and not self._ringed:
            found = await self._find_by_nudge(x, y)
        if not found:
            log.debug("aim (%.3f, %.3f): pointer not found", x, y)
            return
        fx, fy = found
        if max(abs(fx - x), abs(fy - y)) > MAX_JUMP:  # a pointer never misses by this much: a misreading
            log.debug("aim (%.3f, %.3f): ignored a reading at (%.3f, %.3f), too far", x, y, fx, fy)
            return
        log.debug("aim (%.3f, %.3f): pointer at (%.3f, %.3f)", x, y, fx, fy)
        if LEARN_FROM_AIM and max(abs(fx - x), abs(fy - y)) <= MAX_MISS:  # normal drift only, never a stray reading
            self._seen["x"].append((self._fix(x, "x"), fx))
            self._seen["y"].append((self._fix(y, "y"), fy))
            self._save()
        if abs(fx - x) <= AIM_TOLERANCE and abs(fy - y) <= AIM_TOLERANCE:
            return
        dx, dy = round((x - fx) * self.span["w"]), round((y - fy) * self.span["h"])  # fix this miss right away
        await self.device.send(f"move {dx} {dy}")
        self._moved_at = time.time()
        self._pos = (self._pos[0] + dx, self._pos[1] + dy)

    async def _read_pointer(self) -> tuple[float, float] | None:
        """The pointer's place from its ring, once two readings in a row agree.

        The mirrored picture runs up to a second behind the phone, so a frame taken right after a move shows the
        pointer part-way along its path: correcting from that overshoots. Two readings that agree, taken after
        the move had time to show up, mean the picture caught up. None if it never settles in STABLE_MAX.
        """
        self._ringed = False
        started, last = time.time(), None
        while time.time() - started < STABLE_MAX:
            await asyncio.sleep(STABLE_POLL)
            now = vision.pointer_by_color(self.screen.frame)
            self._ringed = self._ringed or now is not None
            if (now and last and abs(now[0] - last[0]) < 0.004 and abs(now[1] - last[1]) < 0.004
                    and time.time() - self._moved_at >= STABLE_AFTER):
                return now
            last = now
        return None

    async def _find_by_nudge(self, x: float, y: float) -> tuple[float, float] | None:
        """Fallback when the pointer has no orange ring: wiggle it and see what moved."""
        nudge = NUDGE if x < 0.9 else -NUDGE  # stay clear of the screen edge
        before, sent = self.screen.frame, time.time()
        await self.device.send(f"move {nudge} 0")
        after = await self.screen.fresh(sent, 0.6)
        if after:
            await self.screen.settled(quiet=0.1, least=0.05, most=0.4)
            after = self.screen.frame
        await self.device.send(f"move {-nudge} 0")
        shift = nudge / self.span["w"]
        return before and after and (vision.pointer(before, after, (x, y), shift)  # near the target first,
                                     or vision.pointer(before, after, (0.5, 0.5), shift, reach=(0.5, 0.5)))  # anywhere

    def _fix(self, t: float, axis: str) -> float:
        """The fraction to aim at so the pointer lands on t, from the line through the observed landings."""
        pts = self._seen[axis]
        if len(pts) < 2:
            return t
        n = len(pts)
        mc, mf = sum(c for c, _ in pts) / n, sum(f for _, f in pts) / n
        var = sum((c - mc) ** 2 for c, _ in pts)
        a = sum((c - mc) * (f - mf) for c, f in pts) / var if var > 1e-4 else 1.0  # landed = a * aimed + b
        a = a if 0.85 < a < 1.15 else 1.0
        b = mf - a * mc
        return (t - b) / a if abs(b) < 0.1 else t

    async def _move_to(self, tx: int, ty: int, rehome: bool = True) -> None:
        if self._link != self.device.links:  # new link: a dropped gesture may have left the button down
            await self.device.send("release")
            self._pos, self._link = None, self.device.links
        if self._pos is None or (rehome and self._moves >= REHOME_EVERY):
            log.debug("pointer homed (believed %s, %d moves since the last home)", self._pos, self._moves)
            await self.device.send("home")
            self._pos, self._moves = (0, 0), 0
        x, y = self._pos
        log.debug("pointer from %s to %s units", (x, y), (tx, ty))
        self._pos = None  # unknown until the move succeeds
        await self.device.send(f"move {tx - x} {ty - y}")
        self._moved_at = time.time()
        self._pos, self._moves = (tx, ty), self._moves + 1

    async def reset(self) -> None:
        """Let go of the button and re-home the pointer, whatever state a gesture left."""
        self._pos = None
        await self.device.send("release")
        await self.device.send("home")
        self._pos, self._moves = (0, 0), 0

    async def tap(self, x: float, y: float, aim: bool = False) -> None:
        """aim=True finds the pointer on screen first (slower); worth it when a tap just missed."""
        await self.point(x, y, aim)
        await self.device.send("click")
        await asyncio.sleep(TAP_GAP)

    async def swipe(self, x1: float, y1: float, x2: float, y2: float, hold: bool = True) -> None:
        await self.drag([(x1, y1), (x2, y2)], hold)

    async def drag(self, points: list[tuple[float, float]], hold: bool = True) -> None:
        """Press at the first point, move through the rest, release: one continuous stroke.

        hold=True rests at the end first, so lists and pickers stop exactly there instead of flinging on.
        System gestures (home, back) need the flick, so they pass hold=False.
        """
        await self.point(*points[0])
        await self.device.send("press")
        try:
            for x, y in points[1:]:
                await self._move_to(self._dx(x), self._dy(y), rehome=False)  # never home mid-drag
            if hold:
                await asyncio.sleep(HOLD)
        finally:
            await self.device.send("release")

    async def scroll(self, x: float, y: float, lines: int) -> None:
        await self.point(x, y)
        await self.device.send(f"scroll {lines} 0")

    async def type(self, text: str) -> None:
        await self.device.send(f"type {text}")

    async def search(self, text: str) -> None:
        """Open Spotlight from anywhere and type `text`: Cmd+H twice reaches a clean home screen (the second one
        closes a Spotlight the first left open), Cmd+Space opens Spotlight with the last query selected, so typing
        replaces it. No pointer is used, so no tap can miss the Search pill or hit the microphone."""
        await self.device.send("key cmd+h")
        await asyncio.sleep(0.6)
        await self.device.send("key cmd+h")
        await asyncio.sleep(0.6)
        await self.device.send("key cmd+space")
        await asyncio.sleep(0.8)
        await self.device.send(f"type {text}")
        await asyncio.sleep(1.0)  # let the results catch up with the typing

    async def key(self, combo: str) -> None:
        await self.device.send(f"key {combo}")

    async def go_home(self) -> None:
        """Cmd+H goes to the home screen from any app and needs no pointer. If the screen does not change
        (no hardware keyboard shortcut, or it was swallowed), swipe up from the bottom edge instead."""
        before, sent = (self.screen.frame if self.screen is not None else None), time.time()
        await self.device.send("key cmd+h")
        if before is None:
            return
        if await self.screen.fresh(sent, 1.2):
            await self.screen.settled(quiet=0.4, least=0.1, most=2)
            if vision.changed(before, self.screen.frame):
                return
        log.info("cmd+h did not change the screen: swiping up from the bottom edge")
        # y=1 puts the pointer on the bottom edge itself (the move is clamped there): the gesture needs the edge
        await self.swipe(0.5, 1.0, 0.5, 0.6, hold=False)  # resting here would open the app switcher

    async def go_back(self) -> None:
        await self.swipe(0.005, 0.45, 0.6, 0.45, hold=False)

    def _units(self, x: float, y: float) -> tuple[int, int]:
        """Mouse units to move from the corner so the pointer lands on (x, y).

        Inside the measured area (self._surface) the landing surface is inverted; elsewhere, and from a
        pointer that is not at the corner, the per-axis line is used.
        """
        s = self._surface
        if s:
            u, v = (self._fix(x, "x") * self.span["w"], self._fix(y, "y") * self.span["h"])
            lo_u, hi_u, lo_v, hi_v = s["box"]
            if lo_u <= u <= hi_u and lo_v <= v <= hi_v:
                for _ in range(8):  # the surface is nearly linear, so a few steps of correction settle it
                    t = _terms(u, v)
                    lx, ly = (sum(c * a for c, a in zip(s[axis], t)) for axis in "xy")
                    u, v = u - (lx - x) * self.span["w"], v - (ly - y) * self.span["h"]
                    u, v = min(max(u, lo_u), hi_u), min(max(v, lo_v), hi_v)
                return round(u), round(v)
        return self._dx(x), self._dy(y)

    def _dx(self, x: float) -> int:
        return round(min(max(self._fix(x, "x"), 0), 1) * self.span["w"])

    def _dy(self, y: float) -> int:
        return round(min(max(self._fix(y, "y"), 0), 1) * self.span["h"])
