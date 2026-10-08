"""Live mouse input for the visible Android Emulator on macOS.

Only the fast Android backend includes this mixin. Quartz posts global macOS
HID events; we verify the emulator window before beginning each gesture.
When no matching window exists, down/move/up fall back to ADB on release.
"""
from __future__ import annotations

import threading
import time
from typing import Optional

import Quartz

from .backend_adb import _adb, _parse_size

_LOCK = threading.RLock()
_TOUCHES: dict[str, dict] = {}
_THRESHOLD = 0.015


def _clamp(v: float) -> float:
    return max(0.0, min(1.0, float(v)))


def _mouse(kind: int, x: float, y: float) -> None:
    event = Quartz.CGEventCreateMouseEvent(
        None, kind, (x, y), Quartz.kCGMouseButtonLeft
    )
    Quartz.CGEventPost(Quartz.kCGHIDEventTap, event)


def _point(rect: dict, nx: float, ny: float) -> tuple[float, float]:
    return (
        rect["x"] + _clamp(nx) * rect["width"],
        rect["y"] + _clamp(ny) * rect["height"],
    )


class QuartzInputMixin:
    """Quartz pointer events; inherited ADB methods handle other controls."""

    def _input_rect(self, udid: str) -> Optional[dict]:
        """Resolve and validate the visible emulator's actual macOS bounds."""
        wid = self._window_id(udid)
        if wid is None or not udid.startswith("emulator-"):
            return None
        port = udid[len("emulator-"):]
        windows = Quartz.CGWindowListCopyWindowInfo(
            Quartz.kCGWindowListOptionOnScreenOnly
            | Quartz.kCGWindowListExcludeDesktopElements,
            Quartz.kCGNullWindowID,
        ) or []
        for win in windows:
            if win.get("kCGWindowNumber") != wid:
                continue
            if not win.get("kCGWindowOwnerName", "").lower().startswith("qemu-system-"):
                continue
            if not win.get("kCGWindowName", "").endswith(":" + port):
                continue
            b = win.get("kCGWindowBounds") or {}
            if b.get("Width", 0) < 100 or b.get("Height", 0) < 100:
                continue
            return {
                "x": float(b["X"]), "y": float(b["Y"]),
                "width": float(b["Width"]), "height": float(b["Height"]),
            }
        return None

    def touch_down(self, udid: str, nx: float, ny: float) -> None:
        with _LOCK:
            previous = _TOUCHES.pop(udid, None)
            if previous and previous["mode"] == "quartz":
                _mouse(Quartz.kCGEventLeftMouseUp, previous["x"], previous["y"])

            nx, ny = _clamp(nx), _clamp(ny)
            rect = self._input_rect(udid)
            if rect is None:
                # ADB fallback if the emulator window is hidden or absent.
                _TOUCHES[udid] = {
                    "mode": "adb", "start": (nx, ny), "last": (nx, ny)
                }
                return

            x, y = _point(rect, nx, ny)
            _mouse(Quartz.kCGEventLeftMouseDown, x, y)
            _TOUCHES[udid] = {
                "mode": "quartz", "rect": rect,
                "x": x, "y": y, "started": time.monotonic(),
            }

    def touch_move(self, udid: str, nx: float, ny: float) -> None:
        with _LOCK:
            state = _TOUCHES.get(udid)
            if state is None:
                return
            nx, ny = _clamp(nx), _clamp(ny)
            if state["mode"] == "adb":
                state["last"] = (nx, ny)
                return
            x, y = _point(state["rect"], nx, ny)
            if (x, y) != (state["x"], state["y"]):
                _mouse(Quartz.kCGEventLeftMouseDragged, x, y)
                state["x"], state["y"] = x, y

    def touch_up(self, udid: str, nx: float, ny: float) -> None:
        with _LOCK:
            state = _TOUCHES.pop(udid, None)
            if state is None:
                return
            nx, ny = _clamp(nx), _clamp(ny)
            if state["mode"] == "quartz":
                x, y = _point(state["rect"], nx, ny)
                if (x, y) != (state["x"], state["y"]):
                    _mouse(Quartz.kCGEventLeftMouseDragged, x, y)
                # Allow even a very fast click to register as a physical press.
                remaining = 0.02 - (time.monotonic() - state["started"])
                if remaining > 0:
                    time.sleep(remaining)
                _mouse(Quartz.kCGEventLeftMouseUp, x, y)
                return

            # Window disappeared/was headless: fall back to a completed ADB
            # gesture instead of silently dropping the user's interaction.
            start_x, start_y = state["start"]
            size = _adb(udid, "shell", "wm", "size", timeout=3)
            dims = _parse_size(size.stdout.decode()) if size.returncode == 0 else None
            if dims is None:
                return
            w, h = dims
            if ((nx - start_x) ** 2 + (ny - start_y) ** 2) ** 0.5 < _THRESHOLD:
                super().tap(udid, nx, ny, w, h)
            else:
                super().drag(udid, start_x, start_y, nx, ny, w, h)

    def touch_cancel(self, udid: str) -> None:
        with _LOCK:
            state = _TOUCHES.pop(udid, None)
            if state is not None and state["mode"] == "quartz":
                _mouse(Quartz.kCGEventLeftMouseUp, state["x"], state["y"])

    def tap(self, udid: str, nx: float, ny: float, dev_w: int, dev_h: int) -> None:
        # Legacy clients also get fast Quartz taps.
        self.touch_down(udid, nx, ny)
        self.touch_up(udid, nx, ny)

    def drag(
        self, udid: str,
        nx1: float, ny1: float, nx2: float, ny2: float,
        dev_w: int, dev_h: int,
    ) -> None:
        # Legacy completed-drag clients: interpolate over ~200 ms.
        self.touch_down(udid, nx1, ny1)
        steps = 12
        for i in range(1, steps + 1):
            t = i / steps
            self.touch_move(
                udid, nx1 + (nx2 - nx1) * t, ny1 + (ny2 - ny1) * t
            )
            time.sleep(0.2 / steps)
        self.touch_up(udid, nx2, ny2)
