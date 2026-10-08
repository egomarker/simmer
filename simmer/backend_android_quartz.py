"""Quartz screenshots of a visible Android Emulator; ADB for all controls.

Fast mode only. Falls back to ordinary ADB screenshot capture when a matching
window is missing or cannot be captured (e.g. headless emulator).
"""
from __future__ import annotations

import re
import time
from typing import Optional

from .backend_adb import AdbBackend


class AndroidQuartzBackend(AdbBackend):
    name = "android (Quartz capture + adb input)"

    def __init__(self) -> None:
        # Serial -> (window ID, timestamp). Discovery is comparatively cheap,
        # but we avoid scanning windows for every frame.
        self._window_ids: dict[str, tuple[int, float]] = {}

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
            import AppKit
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
                    bitmap = AppKit.NSBitmapImageRep.alloc().initWithCGImage_(image)
                    jpeg = bitmap.representationUsingType_properties_(
                        AppKit.NSBitmapImageFileTypeJPEG,
                        {AppKit.NSImageCompressionFactor: max(0, min(100, quality)) / 100.0},
                    )
                    if jpeg:
                        return bytes(jpeg)
        except Exception as exc:
            # A missing/disappearing window should never make streaming fail.
            print(f"[android-quartz] fallback to ADB: {exc}", flush=True)
        return super().capture(udid, quality)
