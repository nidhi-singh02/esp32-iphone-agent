"""iPhone screen via the built-in macOS AirPlay Receiver window.

SCREEN_SOURCE=macos_airplay selects this. It does not launch UxPlay. The mirroring window
must stay visible: capture follows that one window, then drops black letterbox bars so the
JPEG is the phone, at the same 8 fps budget the UxPlay sink uses.

Match the window by owner name (AirPlayUIAgent and similar). Override with
AIRPLAY_WINDOW_OWNER and/or AIRPLAY_WINDOW_TITLE (case-insensitive substrings;
the owner may be a comma-separated list).
"""

import io
import logging
import os
import sys
import threading
import time

from PIL import Image

from .screen import Frames

log = logging.getLogger(__name__)

# Substrings of kCGWindowOwnerName. "airplayui" covers AirPlayUIAgent; the other is the display name
# some macOS versions use for the receiver window.
DEFAULT_OWNER_HINTS = ("airplayui", "airplay receiver")
MIN_SIDE = 80  # points; skips the menu-bar icon and other tiny AirPlay windows
REAL_BAR = 8  # pixels; a thinner edge is capture noise, not a letterbox bar
LETTERBOX_THRESHOLD = 16  # pixel value at or below this counts as black

PERMISSION_HELP = (
    "Screen Recording permission is missing. Allow Terminal under "
    "System Settings, Privacy and Security, Screen Recording, then restart Terminal."
)
WINDOW_HELP = (
    "No AirPlay mirroring window found. Turn on AirPlay Receiver in "
    "System Settings, General, AirDrop and Handoff (AirDrop & Continuity on current macOS), "
    "set Allow AirPlay for to Current User, and mirror the iPhone to this Mac from Control Center. "
    "Keep the mirroring window visible and not minimized. To match a different window, set "
    "AIRPLAY_WINDOW_OWNER or AIRPLAY_WINDOW_TITLE."
)
HIDDEN_HELP = (
    "The AirPlay mirroring window is not on screen. Keep it visible and not minimized, "
    "and leave the Mac unlocked."
)
INSTALL_HELP = (
    'macOS AirPlay capture needs pyobjc. Install it from the server directory: pip install -e ".[mac]"'
)
MAC_ONLY = "SCREEN_SOURCE=macos_airplay only works on macOS."


def load_quartz():
    import Quartz
    return Quartz


def crop_letterbox(image: Image.Image, threshold: int = LETTERBOX_THRESHOLD) -> Image.Image:
    """Drop black bars on one axis (the window is wider or taller than the phone).

    Bars on all four sides are a dark phone screen, not letterboxing, so those stay.
    """
    rgb = image.convert("RGB")
    lut = [0 if i <= threshold else 255 for i in range(256)]
    bbox = rgb.convert("L").point(lut).getbbox()
    if not bbox:
        return image
    width, height = rgb.size
    left, top, right, bottom = bbox
    if (left, top, right, bottom) == (0, 0, width, height):
        return image
    horiz = max(left, width - right) >= REAL_BAR
    vert = max(top, height - bottom) >= REAL_BAR
    if horiz == vert:  # neither, or both: not a single-axis letterbox
        return image
    if right - left < 32 or bottom - top < 32:
        return image
    return rgb.crop(bbox)


def pick_airplay_window(windows, owner: str | None = None, title: str | None = None) -> tuple[dict | None, str]:
    """The largest on-screen match, or (None, help text) when it is missing or hidden."""
    owner_hints, title_hint = _match_rules(owner, title)
    usable = []
    for raw in windows:
        win = _normalize(raw)
        if win is None or not _usable(win):
            continue
        if _matches(win, owner_hints, title_hint):
            usable.append(win)
    if not usable:
        return None, WINDOW_HELP
    onscreen = [win for win in usable if win["onscreen"]]
    if not onscreen:
        return None, HIDDEN_HELP
    return max(onscreen, key=lambda win: win["width"] * win["height"]), ""


def summarize_windows(windows) -> str:
    """Large on-screen windows, so a failed match can name what to put in the override."""
    parts = []
    for raw in windows:
        win = _normalize(raw)
        if win is None or not win["onscreen"] or win["width"] < MIN_SIDE or win["height"] < MIN_SIDE:
            continue
        parts.append(f"{win['owner'] or 'unknown'} ({win['title'] or 'untitled'}, "
                     f"{int(win['width'])}x{int(win['height'])})")
    return "; ".join(parts[:15])


def _match_rules(owner: str | None, title: str | None) -> tuple[tuple[str, ...] | None, str]:
    """Owner hints and a title substring. None owner hints means title-only matching."""
    custom = tuple(part.strip().casefold() for part in (owner or "").split(",") if part.strip())
    title_hint = (title or "").strip().casefold()
    if custom:
        return custom, title_hint
    if title_hint:
        return None, title_hint
    return DEFAULT_OWNER_HINTS, ""


def _matches(win: dict, owner_hints: tuple[str, ...] | None, title_hint: str) -> bool:
    owner, title = win["owner"].casefold(), win["title"].casefold()
    if title_hint and title_hint not in title:
        return False
    if owner_hints is None:
        return True
    return any(hint in owner for hint in owner_hints)


def _usable(win: dict) -> bool:
    return win["id"] > 0 and win["alpha"] > 0 and win["width"] >= MIN_SIDE and win["height"] >= MIN_SIDE


def _normalize(raw) -> dict | None:
    try:
        bounds = _get(raw, "kCGWindowBounds") or {}
        alpha = _get(raw, "kCGWindowAlpha", 1)
        return {
            "id": int(_get(raw, "kCGWindowNumber", 0) or 0),
            "owner": _text(_get(raw, "kCGWindowOwnerName", "")),
            "title": _text(_get(raw, "kCGWindowName", "")),
            "width": float(_get(bounds, "Width", 0) or 0),
            "height": float(_get(bounds, "Height", 0) or 0),
            "onscreen": bool(_get(raw, "kCGWindowIsOnscreen", False)),
            "alpha": float(1 if alpha is None else alpha),
        }
    except (TypeError, ValueError):
        return None


def _get(raw, key, default=None):
    try:
        if isinstance(raw, dict):
            return raw[key] if key in raw else default
        getter = getattr(raw, "get", None)
        if getter is not None:
            return getter(key, default)
        return raw[key]
    except Exception:
        return default


def _text(value) -> str:
    return "" if value is None else str(value)


def _jpeg(image: Image.Image, quality: int) -> bytes:
    buf = io.BytesIO()
    image.save(buf, format="JPEG", quality=quality)
    return buf.getvalue()


def copy_windows(quartz):
    info = quartz.CGWindowListCopyWindowInfo(
        quartz.kCGWindowListOptionAll | quartz.kCGWindowListExcludeDesktopElements, quartz.kCGNullWindowID)
    if info is None:
        raise RuntimeError("Could not list windows (no window server). " + PERMISSION_HELP)
    return list(info)


def grab_window(quartz, window_id: int) -> Image.Image | None:
    """One window's pixels, without its frame or shadow, at the screen's full resolution."""
    options = (quartz.kCGWindowImageBoundsIgnoreFraming | quartz.kCGWindowImageBestResolution
               | quartz.kCGWindowImageShouldBeOpaque)
    image = quartz.CGWindowListCreateImageFromArray(quartz.CGRectNull, [window_id], options)
    if image is None:
        return None
    return _cgimage_to_pil(quartz, image)


def recording_ok(quartz) -> bool:
    preflight = getattr(quartz, "CGPreflightScreenCaptureAccess", None)
    if preflight is None:
        return True
    if preflight():
        return True
    # Puts Terminal in the Screen Recording list. The grant applies after Terminal is restarted.
    request = getattr(quartz, "CGRequestScreenCaptureAccess", None)
    if request is not None:
        request()
    return False


def _cgimage_to_pil(quartz, image) -> Image.Image:
    width, height = int(quartz.CGImageGetWidth(image)), int(quartz.CGImageGetHeight(image))
    stride = int(quartz.CGImageGetBytesPerRow(image))
    bits = int(quartz.CGImageGetBitsPerPixel(image))
    if width <= 0 or height <= 0:
        raise RuntimeError("AirPlay window capture was empty")
    if bits != 32:
        raise RuntimeError(f"AirPlay window capture was {bits}-bit, expected 32-bit")
    info = int(quartz.CGImageGetBitmapInfo(image))
    alpha = info & int(quartz.kCGBitmapAlphaInfoMask)
    little = (info & int(quartz.kCGBitmapByteOrderMask)) == int(quartz.kCGBitmapByteOrder32Little)
    first = (quartz.kCGImageAlphaPremultipliedFirst, quartz.kCGImageAlphaNoneSkipFirst, quartz.kCGImageAlphaFirst)
    rawmode = "BGRA" if alpha in first and little else "RGBA"
    data = quartz.CGDataProviderCopyData(quartz.CGImageGetDataProvider(image))
    return Image.frombytes("RGBA", (width, height), bytes(data), "raw", rawmode, stride, 1)


class MacAirPlayScreen(Frames):
    """Captures the AirPlay Receiver window. Same frame, aspect, settled, and fresh contract as UxPlay."""

    def __init__(self, fps: int = 8, quality: int = 75, owner: str | None = None, title: str | None = None, *,
                 windows=None, grab=None, recording_allowed=None):
        super().__init__()
        self.fps, self.quality = fps, quality
        self.owner, self.title = owner, title
        self.error: str | None = None
        self._windows, self._grab, self._recording_allowed = windows, grab, recording_allowed
        self._pixels: bytes | None = None
        self._seen_at = 0.0
        self._window_id = None
        self._grab_misses = 0
        self._stopping = False
        self._thread: threading.Thread | None = None

    @classmethod
    def from_env(cls) -> "MacAirPlayScreen":
        def env(name: str) -> str | None:
            value = os.environ.get(name, "").strip()
            return value or None
        return cls(owner=env("AIRPLAY_WINDOW_OWNER"), title=env("AIRPLAY_WINDOW_TITLE"))

    @property
    def live(self) -> bool:
        # A still phone keeps mirroring; UxPlay on macOS only infers that from a recent frame.
        # Here the window is still being captured, so live stays true until the capture stops.
        return self.frame is not None and time.time() - self._seen_at < 2

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stopping = False
        self._thread = threading.Thread(target=self._run, name="macos-airplay", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stopping = True
        if self._thread is not None:
            self._thread.join(timeout=2)

    def _run(self) -> None:
        if not self._ensure_backends():
            return
        if not self._recording_allowed():
            self._fail(PERMISSION_HELP)
            return
        while not self._stopping:
            started = time.time()
            try:
                windows = list(self._windows() or [])
                window, message = pick_airplay_window(windows, self.owner, self.title)
            except Exception as exc:
                self.frame, self._pixels, self._seen_at = None, None, 0.0
                self._fail(f"Could not list windows: {exc}")
                self._sleep(1)
                continue
            if window is None:
                self._lost(message, windows)
                self._sleep(1)
                continue
            self._capture(window)
            self._sleep(max(0.0, 1 / self.fps - (time.time() - started)))

    def _ensure_backends(self) -> bool:
        if self._windows is not None and self._grab is not None and self._recording_allowed is not None:
            return True
        if sys.platform != "darwin":
            self._fail(MAC_ONLY)
            return False
        try:
            quartz = load_quartz()
        except ImportError:
            self._fail(INSTALL_HELP)
            return False
        if self._windows is None:
            self._windows = lambda: copy_windows(quartz)
        if self._grab is None:
            self._grab = lambda window_id: grab_window(quartz, window_id)
        if self._recording_allowed is None:
            self._recording_allowed = lambda: recording_ok(quartz)
        return True

    def _capture(self, window: dict) -> None:
        if window["id"] != self._window_id:
            log.info("AirPlay mirroring window found: %s (%s)", window["owner"], window["title"] or "untitled")
            self._window_id = window["id"]
            self.error = None
        try:
            image = self._grab(window["id"])
        except Exception as exc:
            log.warning("AirPlay window capture failed: %s", exc)
            image = None
        if image is None:
            self._grab_misses += 1
            if self._grab_misses == 3:
                self._fail("Capturing the AirPlay window returned nothing. " + PERMISSION_HELP)
            return
        try:
            rgb = crop_letterbox(image).convert("RGB")
        except Exception as exc:
            log.warning("AirPlay window capture failed: %s", exc)
            return
        self._grab_misses = 0
        self.error = None
        pixels = rgb.tobytes()
        if pixels != self._pixels:  # identical pixels are not a new frame, so settled() and fresh() match UxPlay
            self._pixels = pixels
            self.frame = _jpeg(rgb, self.quality)
            self.updated = time.time()
        self._seen_at = time.time()

    def _lost(self, message: str, windows) -> None:
        self.frame = None
        self._pixels = None
        self._seen_at = 0.0
        self._window_id = None
        self._grab_misses = 0
        if self.error == message:
            return
        self.error = message
        log.error("%s", message)
        log.error("On-screen windows: %s", summarize_windows(windows) or "(none)")

    def _fail(self, message: str) -> None:
        if self.error != message:
            self.error = message
            log.error("%s", message)

    def _sleep(self, seconds: float) -> None:
        end = time.time() + seconds
        while not self._stopping and time.time() < end:
            time.sleep(min(0.05, end - time.time()))
