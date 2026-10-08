"""Fast backend: Quartz window capture + CGEvent injection.
Requires Screen Recording and Accessibility permissions.
"""

from __future__ import annotations

import json
import subprocess
import time
import threading
from pathlib import Path
from typing import Optional

import AppKit
import Quartz

from .backend_base import SimDevice
from .backend_ios import press_home

name = "fast (Quartz)"


# ── Sim discovery ──────────────────────────────────────────────────────────────


_BOOTED_CACHE: dict[str, str] = {}
_BOOTED_CACHE_TIME = 0.0
_BOOTED_CACHE_TTL = 20.0  # seconds


def _booted_udids() -> dict[str, str]:
    global _BOOTED_CACHE, _BOOTED_CACHE_TIME

    now = time.monotonic()
    if now - _BOOTED_CACHE_TIME < _BOOTED_CACHE_TTL:
        return _BOOTED_CACHE

    result = subprocess.run(
        ["xcrun", "simctl", "list", "devices", "--json"],
        capture_output=True,
        text=True,
        check=True,
    )

    out: dict[str, str] = {}
    for devices in json.loads(result.stdout).get("devices", {}).values():
        for dev in devices:
            if dev.get("state") == "Booted":
                out[dev["name"]] = dev["udid"]

    _BOOTED_CACHE = out
    _BOOTED_CACHE_TIME = now
    return out


def _quartz_windows() -> list[dict]:
    window_list = Quartz.CGWindowListCopyWindowInfo(
        Quartz.kCGWindowListOptionOnScreenOnly | Quartz.kCGWindowListExcludeDesktopElements,
        Quartz.kCGNullWindowID,
    )
    out = []
    for w in window_list:
        if w.get("kCGWindowOwnerName") != "Simulator":
            continue
        name_ = w.get("kCGWindowName", "")
        if not name_:
            continue
        wid = w.get("kCGWindowNumber")
        bounds = w.get("kCGWindowBounds")
        if not (wid and bounds):
            continue
        width, height = int(bounds["Width"]), int(bounds["Height"])
        if width < 100 or height < 100:
            continue
        out.append(
            {
                "name": name_,
                "wid": wid,
                "x": int(bounds["X"]),
                "y": int(bounds["Y"]),
                "width": width,
                "height": height,
            }
        )
    return out


def list_sims() -> list[SimDevice]:
    udids = _booted_udids()
    sims = []
    for win in _quartz_windows():
        udid = udids.get(win["name"], f"win_{win['wid']}")
        sims.append(SimDevice(udid=udid, name=win["name"], width=win["width"], height=win["height"]))
    return sims


def _find_window(udid: str) -> Optional[dict]:
    udids = _booted_udids()
    name_for_udid = next((n for n, u in udids.items() if u == udid), None)
    if not name_for_udid:
        return None
    return next((w for w in _quartz_windows() if w["name"] == name_for_udid), None)


# ── Capture ────────────────────────────────────────────────────────────────────



def capture(udid: str, quality: int = 70) -> Optional[bytes]:
    pool = AppKit.NSAutoreleasePool.alloc().init()

    try:
        win = _find_window(udid)
        if not win:
            return None

        image = Quartz.CGWindowListCreateImage(
            Quartz.CGRectNull,
            Quartz.kCGWindowListOptionIncludingWindow,
            win["wid"],
            Quartz.kCGWindowImageBoundsIgnoreFraming
            | Quartz.kCGWindowImageNominalResolution,
        )
        if image is None:
            return None

        bitmap = AppKit.NSBitmapImageRep.alloc().initWithCGImage_(image)
        jpeg_data = bitmap.representationUsingType_properties_(
            AppKit.NSBitmapImageFileTypeJPEG,
            {AppKit.NSImageCompressionFactor: quality / 100.0},
        )

        return bytes(jpeg_data) if jpeg_data is not None else None

    finally:
        del pool



# ── Input injection ────────────────────────────────────────────────────────────


def _activate() -> None:
    apps = AppKit.NSRunningApplication.runningApplicationsWithBundleIdentifier_("com.apple.iphonesimulator")
    if apps:
        apps[0].activateWithOptions_(AppKit.NSApplicationActivateIgnoringOtherApps)


def _mouse(event_type: int, x: float, y: float) -> None:
    event = Quartz.CGEventCreateMouseEvent(None, event_type, (x, y), Quartz.kCGMouseButtonLeft)
    Quartz.CGEventPost(Quartz.kCGHIDEventTap, event)



# Single-pointer Quartz gesture state (per simulator).
# CGEvent uses global macOS mouse coordinates, as in the existing tap()/drag().

_LIVE_TOUCH_LOCK = threading.RLock()
_LIVE_TOUCHES: dict[str, dict] = {}


def _live_point(win: dict, nx: float, ny: float) -> tuple[float, float]:
    nx = max(0.0, min(1.0, float(nx)))
    ny = max(0.0, min(1.0, float(ny)))
    return win["x"] + nx * win["width"], win["y"] + ny * win["height"]


def touch_down(udid: str, nx: float, ny: float) -> None:
    with _LIVE_TOUCH_LOCK:
        # Recover if the previous WebSocket disappeared mid-gesture.
        previous = _LIVE_TOUCHES.pop(udid, None)
        if previous:
            _mouse(Quartz.kCGEventLeftMouseUp, previous["x"], previous["y"])
        win = _find_window(udid)
        if win is None:
            return
        x, y = _live_point(win, nx, ny)
        _mouse(Quartz.kCGEventLeftMouseDown, x, y)
        _LIVE_TOUCHES[udid] = {
            "win": win, "x": x, "y": y, "started": time.monotonic(),
        }


def touch_move(udid: str, nx: float, ny: float) -> None:
    with _LIVE_TOUCH_LOCK:
        state = _LIVE_TOUCHES.get(udid)
        if state is None:
            return
        x, y = _live_point(state["win"], nx, ny)
        if (x, y) != (state["x"], state["y"]):
            _mouse(Quartz.kCGEventLeftMouseDragged, x, y)
            state["x"], state["y"] = x, y


def touch_up(udid: str, nx: float, ny: float) -> None:
    with _LIVE_TOUCH_LOCK:
        state = _LIVE_TOUCHES.pop(udid, None)
        if state is None:
            return
        x, y = _live_point(state["win"], nx, ny)
        # Very quick taps still need a small physical press duration.
        remaining = 0.02 - (time.monotonic() - state["started"])
        if remaining > 0:
            time.sleep(remaining)
        _mouse(Quartz.kCGEventLeftMouseUp, x, y)


def touch_cancel(udid: str) -> None:
    with _LIVE_TOUCH_LOCK:
        state = _LIVE_TOUCHES.pop(udid, None)
        if state is not None:
            _mouse(Quartz.kCGEventLeftMouseUp, state["x"], state["y"])



def tap(udid: str, nx: float, ny: float, dev_w: int, dev_h: int) -> None:
    t0 = time.perf_counter()

    win = _find_window(udid)
    t1 = time.perf_counter()
    if not win:
        print("[tap-profile] Window not found", flush=True)
        return

    x = win["x"] + nx * win["width"]
    y = win["y"] + ny * win["height"]

    #_activate()
    t2 = time.perf_counter()

    #time.sleep(0.05)
    t3 = time.perf_counter()

    _mouse(Quartz.kCGEventLeftMouseDown, x, y)
    t4 = time.perf_counter()

    time.sleep(0.02)
    t5 = time.perf_counter()

    _mouse(Quartz.kCGEventLeftMouseUp, x, y)
    t6 = time.perf_counter()

    ms = lambda a, b: (b - a) * 1000
    print(
        f"[tap-profile] "
        f"lookup={ms(t0,t1):.1f}ms "
        f"activate={ms(t1,t2):.1f}ms "
        f"wait1={ms(t2,t3):.1f}ms "
        f"down={ms(t3,t4):.1f}ms "
        f"wait2={ms(t4,t5):.1f}ms "
        f"up={ms(t5,t6):.1f}ms "
        f"TOTAL={ms(t0,t6):.1f}ms",
        flush=True,
    )



def drag(
    udid: str,
    nx1: float,
    ny1: float,
    nx2: float,
    ny2: float,
    dev_w: int,
    dev_h: int,
    steps: int = 25,
    duration: float = 0.3,
) -> None:
    win = _find_window(udid)
    if not win:
        return
    x1 = win["x"] + nx1 * win["width"]
    y1 = win["y"] + ny1 * win["height"]
    x2 = win["x"] + nx2 * win["width"]
    y2 = win["y"] + ny2 * win["height"]
    _activate()
    time.sleep(0.05)
    _mouse(Quartz.kCGEventLeftMouseDown, x1, y1)
    step_delay = duration / steps
    for i in range(1, steps + 1):
        t = i / steps
        _mouse(Quartz.kCGEventLeftMouseDragged, x1 + (x2 - x1) * t, y1 + (y2 - y1) * t)
        time.sleep(step_delay)
    _mouse(Quartz.kCGEventLeftMouseUp, x2, y2)


_KEY_CODES: dict[str, int] = {
    "backspace": 51,
    "delete": 51,
    "return": 36,
    "enter": 36,
    "tab": 48,
    "escape": 53,
    "arrowleft": 123,
    "arrowright": 124,
    "arrowdown": 125,
    "arrowup": 126,
    "home": 115,
    "end": 119,
    "pageup": 116,
    "pagedown": 121,
}
_CMD, _V = 55, 9


def _key_event(code: int, down: bool, flags: int = 0) -> None:
    event = Quartz.CGEventCreateKeyboardEvent(None, code, down)
    if flags:
        Quartz.CGEventSetFlags(event, flags)
    Quartz.CGEventPost(Quartz.kCGHIDEventTap, event)
    time.sleep(0.01)


def key(udid: str, k: str) -> None:
    code = _KEY_CODES.get(k.lower())
    if code is None:
        return
    _key_event(code, True)
    _key_event(code, False)


def text(udid: str, t: str) -> None:
    subprocess.run(["pbcopy"], input=t.encode("utf-8"), check=True)
    _activate()
    time.sleep(0.1)
    _key_event(_CMD, True)
    _key_event(_V, True, Quartz.kCGEventFlagMaskCommand)
    _key_event(_V, False, Quartz.kCGEventFlagMaskCommand)
    _key_event(_CMD, False)


def home(udid: str) -> bool:
    if press_home(udid):
        return True
    _activate()
    # Last fallback: Simulator shortcut (requires Accessibility).
    _H = 4
    flags = Quartz.kCGEventFlagMaskCommand | Quartz.kCGEventFlagMaskShift
    _key_event(_CMD, True)
    _key_event(56, True)  # left shift
    _key_event(_H, True, flags)
    _key_event(_H, False, flags)
    _key_event(56, False)
    _key_event(_CMD, False)
    return True


_ROTATE_LOG = "/tmp/simmer_rotate.log"


def _rlog(*args) -> None:
    import datetime

    msg = " ".join(str(a) for a in args)
    line = f"{datetime.datetime.now().strftime('%H:%M:%S.%f')[:-3]} {msg}\n"
    print(line, end="", flush=True)
    with open(_ROTATE_LOG, "a") as f:
        f.write(line)


def rotate(udid: str) -> bool:
    Path(_ROTATE_LOG).write_text("")  # clear log at start of each rotate attempt
    win = _find_window(udid)
    _rlog(f"window={'found' if win else 'NOT FOUND'} udid={udid[:8]}")
    if win:
        _rlog(f"  bounds x={win['x']} y={win['y']} w={win['width']} h={win['height']}")

    _activate()
    time.sleep(0.15)

    if win:
        cx = win["x"] + win["width"] // 2
        ty = win["y"] + 8
        _rlog(f"clicking window top to focus ({cx:.0f}, {ty:.0f})")
        _mouse(Quartz.kCGEventLeftMouseDown, cx, ty)
        time.sleep(0.05)
        _mouse(Quartz.kCGEventLeftMouseUp, cx, ty)
        time.sleep(0.2)
    else:
        _rlog("no window bounds — skipping focus click")

    _rlog("sending Cmd+Left via HID tap")
    LEFT = 123
    _key_event(_CMD, True)
    _key_event(LEFT, True, Quartz.kCGEventFlagMaskCommand)
    _key_event(LEFT, False, Quartz.kCGEventFlagMaskCommand)
    _key_event(_CMD, False)
    _rlog("Cmd+Left done")
    return True


def appearance(udid: str, mode: str) -> None:
    subprocess.run(["xcrun", "simctl", "ui", udid, "appearance", mode], capture_output=True)
