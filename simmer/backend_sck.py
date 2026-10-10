"""Opt-in --fast2: ScreenCaptureKit capture with the existing fast-mode inputs."""
from __future__ import annotations

import subprocess
import threading
import time

from .screen_capture import CaptureService
from .ios_viewport import REFRESH_SECONDS, ViewportCache


_NATIVE_ROTATE_SCRIPT = 'on run argv\n    set deviceName to item 1 of argv\n    set directionName to item 2 of argv\n\n    if directionName is "left" then\n        set menuLabel to "Rotate Left"\n    else\n        set menuLabel to "Rotate Right"\n    end if\n\n    tell application id "com.apple.iphonesimulator" to activate\n    tell application "System Events"\n        tell application process "Simulator"\n            set frontmost to true\n            set targetWindow to missing value\n            repeat with candidate in windows\n                try\n                    if (name of candidate as text) contains deviceName then\n                        set targetWindow to candidate\n                        exit repeat\n                    end if\n                end try\n            end repeat\n            if targetWindow is missing value then\n                error "Could not find Simulator window: " & deviceName\n            end if\n\n            perform action "AXRaise" of targetWindow\n            delay 0.20\n            click menu item menuLabel of menu 1 of menu bar item "Device" of menu bar 1\n        end tell\n    end tell\nend run'


class IOSScreenCaptureBackend:
    name = "fast2 (ScreenCaptureKit + Quartz input)"
    native_ios_rotation = True
    graceful_shutdown = True

    def __init__(self, service: CaptureService):
        from . import backend_quartz

        self._input = backend_quartz
        self._service = service
        self._viewport = ViewportCache()
        self._list_lock = threading.RLock()
        self._sims_cache = []
        self._sims_checked = 0.0

    def __getattr__(self, name):
        # Discovery, live touch, taps, keys, text, home, rotation and appearance
        # remain exactly the Quartz backend's implementations.
        return getattr(self._input, name)

    def rotate_native(self, udid):
        """Use Simulator's Device menu; return its new landscape state or None.

        Runs in the input executor, never on the asyncio event loop. The
        window-orientation check guards against treating a successful
        osascript exit as proof that the Simulator actually rotated.
        """
        window = self._input._find_window(udid)
        if not window:
            return None
        was_landscape = window["width"] > window["height"]
        # Left from portrait, right to return to portrait (not upside-down).
        direction = "right" if was_landscape else "left"
        try:
            command = subprocess.run(
                ["osascript", "-", window["name"], direction],
                input=_NATIVE_ROTATE_SCRIPT,
                capture_output=True,
                text=True,
                timeout=8,
            )
            if command.returncode != 0:
                return None
            # Rotation is animated; wait briefly for real host geometry.
            deadline = time.monotonic() + 3.0
            while time.monotonic() < deadline:
                current = self._input._find_window(udid)
                if current and ((current["width"] > current["height"]) != was_landscape):
                    return not was_landscape
                time.sleep(0.10)
        except (OSError, subprocess.TimeoutExpired):
            pass
        return None  # XCTest fallback if the menu did not rotate the window.

    def list_sims(self):
        # /api/sims can be polled frequently; do not scan Quartz/AX more than
        # once per 5 seconds even during multiple simultaneous client requests.
        with self._list_lock:
            now = time.monotonic()
            if now - self._sims_checked < REFRESH_SECONDS:
                return list(self._sims_cache)
            sims = self._input.list_sims()
            for sim in sims:
                window = self._viewport.get(sim.udid, self._input._find_window)
                if window is not None and window.get("crop"):
                    sim.width, sim.height = window["crop"][2:]
            self._sims_cache = sims
            self._sims_checked = now
            return list(sims)

    def _find_window(self, udid, refresh=False):
        return self._viewport.get(udid, self._input._find_window, refresh=refresh)

    def invalidate_viewport(self, udid):
        self._viewport.invalidate(udid)
        with self._list_lock:
            self._sims_checked = 0.0
        self._service.invalidate(udid)

    def _point(self, udid, x, y):
        viewport = getattr(self, "_viewport", None)
        return viewport.point(udid, x, y) if viewport else (x, y)

    def touch_down(self, udid, x, y):
        self._input.touch_down(udid, *self._point(udid, x, y))

    def touch_move(self, udid, x, y):
        self._input.touch_move(udid, *self._point(udid, x, y))

    def touch_up(self, udid, x, y):
        self._input.touch_up(udid, *self._point(udid, x, y))

    def tap(self, udid, x, y, dev_w, dev_h):
        self._input.tap(udid, *self._point(udid, x, y), dev_w, dev_h)

    def drag(self, udid, x1, y1, x2, y2, dev_w, dev_h):
        x1, y1 = self._point(udid, x1, y1)
        x2, y2 = self._point(udid, x2, y2)
        self._input.drag(udid, x1, y1, x2, y2, dev_w, dev_h)

    def capture(self, udid, quality=70):
        return self._service.capture(udid, quality, self._find_window)

    async def startup(self):
        await self._service.startup()

    async def shutdown(self):
        await self._service.shutdown()


class AndroidScreenCaptureBackend(IOSScreenCaptureBackend):
    name = "Android (fast2 ScreenCaptureKit + Quartz touch + ADB controls)"
    native_ios_rotation = False

    def __init__(self, service: CaptureService):
        from .backend_android_quartz import AndroidQuartzBackend

        self._input = AndroidQuartzBackend()
        self._service = service

    def list_sims(self):
        return self._input.list_sims()

    def invalidate_viewport(self, udid):
        self._service.invalidate(udid)

    def _find_window(self, udid, refresh=False):
        rect = self._input._input_rect(udid)
        if rect is None:
            return None

        return {
            "wid": self._input._window_id(udid),
            **rect,
            "include_child_windows": False,
        }
