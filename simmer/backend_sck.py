"""Opt-in --fast2: ScreenCaptureKit capture with the existing fast-mode inputs."""
from __future__ import annotations

from .screen_capture import CaptureService


class IOSScreenCaptureBackend:
    name = "fast2 (ScreenCaptureKit + Quartz input)"
    graceful_shutdown = True

    def __init__(self, service: CaptureService):
        from . import backend_quartz

        self._input = backend_quartz
        self._service = service

    def __getattr__(self, name):
        # Discovery, live touch, taps, keys, text, home, rotation and appearance
        # remain exactly the Quartz backend's implementations.
        return getattr(self._input, name)

    def _find_window(self, udid):
        return self._input._find_window(udid)

    def capture(self, udid, quality=70):
        return self._service.capture(udid, quality, self._find_window)

    async def startup(self):
        await self._service.startup()

    async def shutdown(self):
        await self._service.shutdown()


class AndroidScreenCaptureBackend(IOSScreenCaptureBackend):
    name = "Android (fast2 ScreenCaptureKit + Quartz touch + ADB controls)"

    def __init__(self, service: CaptureService):
        from .backend_android_quartz import AndroidQuartzBackend

        self._input = AndroidQuartzBackend()
        self._service = service

    def _find_window(self, udid):
        rect = self._input._input_rect(udid)
        if rect is None:
            return None
        return {"wid": self._input._window_id(udid), **rect}
