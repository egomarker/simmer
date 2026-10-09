"""Quartz screenshots of a visible Android Emulator; ADB for all controls.

Fast mode only. Falls back to ordinary ADB screenshot capture when a matching
window is missing or cannot be captured (e.g. headless emulator).
"""
from __future__ import annotations

import re
import time
from typing import Optional

from .backend_adb import AdbBackend


from .backend_android_input_quartz import QuartzInputMixin
from .quartz_jpeg import encode_jpeg

class AndroidQuartzBackend(QuartzInputMixin, AdbBackend):
    name = "android (Quartz capture + Quartz touch + adb controls)"

    def __init__(self) -> None:
        # Serial -> (window ID, timestamp). Discovery is comparatively cheap,
        # but we avoid scanning windows for every frame.
        self._window_ids: dict[str, tuple[int, float]] = {}

    def list_sims(self):
        sims = super().list_sims()

        for sim in sims:
            try:
                rect = self._input_rect(sim.udid)
            except Exception:
                rect = None

            if rect is not None:
                sim.width = int(round(rect["width"]))
                sim.height = int(round(rect["height"]))

        return sims

    def _window_id(self, udid: str) -> Optional[int]:
        match = re.fullmatch(r"emulator-(\d+)", udid)
        if not match:
            return None
        now = time.monotonic()
        cached = self._window_ids.get(udid)
        if cached and now - cached[1] < 2.0:
            return cached[0]

        import Quartz

        windows = Quartz.CGWindowListCopyWindowInfo(
            Quartz.kCGWindowListOptionOnScreenOnly
            | Quartz.kCGWindowListExcludeDesktopElements,
            Quartz.kCGNullWindowID,
        ) or []
        suffix = ":" + match.group(1)
        candidates = []
        for win in windows:
            owner = win.get("kCGWindowOwnerName", "").lower()
            title = win.get("kCGWindowName", "")
            bounds = win.get("kCGWindowBounds") or {}
            wid = win.get("kCGWindowNumber")
            if not owner.startswith("qemu-system-"):
                continue
            if not title.endswith(suffix) or not wid:
                continue
            if bounds.get("Width", 0) < 100 or bounds.get("Height", 0) < 100:
                continue
            candidates.append((bounds["Width"] * bounds["Height"], int(wid)))

        if not candidates:
            self._window_ids.pop(udid, None)
            return None
        wid = max(candidates)[1]
        self._window_ids[udid] = (wid, now)
        return wid

    def capture(self, udid: str, quality: int) -> Optional[bytes]:
        try:
            import Quartz

            wid = self._window_id(udid)
            if wid is not None:
                image = Quartz.CGWindowListCreateImage(
                    Quartz.CGRectNull,
                    Quartz.kCGWindowListOptionIncludingWindow,
                    wid,
                    Quartz.kCGWindowImageBoundsIgnoreFraming
                    | Quartz.kCGWindowImageNominalResolution,
                )
                if image is None:
                    # Window ID may have changed; rediscover next time.
                    self._window_ids.pop(udid, None)
                else:
                    frame = encode_jpeg(image, quality)
                    if frame:
                        return frame
        except Exception as exc:
            # A missing/disappearing window should never make streaming fail.
            print(f"[android-quartz] fallback to ADB: {exc}", flush=True)
        return super().capture(udid, quality)
