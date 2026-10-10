"""iOS Simulator display-only geometry for the ScreenCaptureKit backend.

Quartz locates the host window; AXSubrole=iOSContentGroup identifies the
actual screen inside its titlebar and bezel. Nothing is queried per frame.
"""
from __future__ import annotations

import re
import threading
import time
from collections import deque

REFRESH_SECONDS = 5.0


def _ax_value(element, attribute):
    from ApplicationServices import AXUIElementCopyAttributeValue
    try:
        error, value = AXUIElementCopyAttributeValue(element, attribute, None)
        return value if error == 0 else None
    except Exception:
        return None


def _ax_frame(element):
    """Parse PyObjC AXValue CGPoint and CGSize (verified with AX Inspector)."""
    position = _ax_value(element, "AXPosition")
    size = _ax_value(element, "AXSize")
    if position is None or size is None:
        return None

    def number(value, key):
        m = re.search(rf"\b{key}:\s*(-?\d+(?:\.\d+)?)", str(value))
        return float(m.group(1)) if m else None

    rect = (number(position, "x"), number(position, "y"),
            number(size, "w"), number(size, "h"))
    return rect if all(x is not None for x in rect) else None


def find_display_crop(window):
    """Return x/y-from-top, width/height inside a Quartz window, or None.

    Handles missing AX permissions/framework by falling back to full-window
    capture. Called only on initial lookup, geometry change or invalidation.
    """
    try:
        import Quartz
        from ApplicationServices import (
            AXIsProcessTrusted, AXUIElementCreateApplication,
            AXUIElementSetMessagingTimeout,
        )
        if not AXIsProcessTrusted():
            return None
        infos = Quartz.CGWindowListCopyWindowInfo(
            Quartz.kCGWindowListOptionIncludingWindow, int(window["wid"]),
        ) or []
        match = next((w for w in infos if int(w.get("kCGWindowNumber", -1)) == int(window["wid"])), None)
        if match is None or match.get("kCGWindowOwnerName") != "Simulator":
            return None
        pid = int(match["kCGWindowOwnerPID"])
        app = AXUIElementCreateApplication(pid)
        AXUIElementSetMessagingTimeout(app, 0.5)
        roots = _ax_value(app, "AXWindows") or []
        wx, wy = float(window["x"]), float(window["y"])
        ww, wh = float(window["width"]), float(window["height"])
        title = str(window.get("name", "")).lower()
        for root in roots:
            frame = _ax_frame(root)
            if frame is None:
                continue
            rx, ry, rw, rh = frame
            if (abs(rx - wx) > 5 or abs(ry - wy) > 5 or
                    abs(rw - ww) > 5 or abs(rh - wh) > 5):
                continue  # do not mix up multiple open Simulator windows
            ax_title = str(_ax_value(root, "AXTitle") or "").lower()
            if title and title not in ax_title:
                continue
            queue = deque([(root, 0)])
            visited = 0
            while queue and visited < 200:
                element, depth = queue.popleft()
                visited += 1
                if str(_ax_value(element, "AXSubrole") or "") == "iOSContentGroup":
                    rect = _ax_frame(element)
                    if rect is not None:
                        x, y, w, h = rect
                        ox, oy = round(x - wx), round(y - wy)
                        cw, ch = round(w), round(h)
                        if (0 <= ox < ww and 0 <= oy < wh and
                                cw >= ww * 0.4 and ch >= wh * 0.4 and
                                ox + cw <= ww + 2 and oy + ch <= wh + 2):
                            return (ox, oy, min(cw, round(ww) - ox), min(ch, round(wh) - oy))
                if depth < 12:
                    queue.extend((child, depth + 1)
                                 for child in (_ax_value(element, "AXChildren") or []))
    except (ImportError, KeyError, ValueError, TypeError, OSError):
        return None
    return None


def pixel_crop(crop, target_width, target_height, pixel_width, pixel_height):
    """Convert top-left Quartz window crop to bottom-left CoreImage pixel ROI."""
    if crop is None:
        return (0, 0, pixel_width, pixel_height)
    x, y, w, h = crop
    sx, sy = pixel_width / target_width, pixel_height / target_height
    left = max(0, min(pixel_width - 1, round(x * sx)))
    top = max(0, min(pixel_height - 1, round(y * sy)))
    right = max(left + 1, min(pixel_width, round((x + w) * sx)))
    bottom = max(top + 1, min(pixel_height, round((y + h) * sy)))
    return (left, pixel_height - bottom, right - left, bottom - top)


class ViewportCache:
    def __init__(self):
        self._lock = threading.RLock()
        self._entries = {}

    def invalidate(self, udid):
        with self._lock:
            self._entries.pop(udid, None)

    def cached(self, udid):
        """For input: do not invoke Quartz or AX while processing a gesture."""
        with self._lock:
            return self._entries.get(udid, (None, 0))[0]

    def get(self, udid, find_window, *, refresh=False):
        with self._lock:
            now = time.monotonic()
            previous, checked = self._entries.get(udid, (None, 0))
            if not refresh and now - checked < REFRESH_SECONDS:
                return previous

            window = find_window(udid)
            if window is None:
                self._entries[udid] = (None, now)
                return None

            geometry = tuple(window.get(k) for k in ("wid", "x", "y", "width", "height"))
            previous_geometry = (tuple(previous.get(k) for k in ("wid", "x", "y", "width", "height"))
                                 if previous is not None else None)
            if refresh or geometry != previous_geometry or previous.get("crop") is None:
                crop = find_display_crop(window)
            else:
                crop = previous["crop"]
            snapshot = {**window, "crop": crop}
            self._entries[udid] = (snapshot, now)
            return snapshot

    def point(self, udid, nx, ny):
        """Map display-normalized coordinates into original window-normalized coordinates."""
        snapshot = self.cached(udid)
        nx, ny = max(0., min(1., float(nx))), max(0., min(1., float(ny)))
        if not snapshot or not snapshot.get("crop"):
            return nx, ny
        x, y, w, h = snapshot["crop"]
        return ((x + nx * w) / snapshot["width"],
                (y + ny * h) / snapshot["height"])
