"""Bounded, desktop-independent ScreenCaptureKit window capture for --fast2.

Native setup/completions run on asyncio's main thread. Only JPEG encoding runs
in the server's capture worker. PyObjC owns all CF proxies: no manual releases.
"""
from __future__ import annotations

import asyncio
import logging
import platform
import sys
import threading
import time
from dataclasses import dataclass
from types import SimpleNamespace

from .quartz_jpeg import encode_jpeg

log = logging.getLogger(__name__)
_OUTPUT_CLASS = None


def pooled(fn, *args):
    from Foundation import NSAutoreleasePool

    pool = NSAutoreleasePool.alloc().init()
    try:
        return fn(*args)
    finally:
        del pool


def load_api():
    if sys.platform != "darwin" or int(platform.mac_ver()[0].split(".")[0] or 0) < 14:
        raise RuntimeError("--fast2 requires macOS 14 or later; use --mode fast on older macOS")
    if threading.current_thread() is not threading.main_thread():
        raise RuntimeError("ScreenCaptureKit must be initialized on the main thread")
    try:
        import objc
        import Foundation
        import Quartz

        # Fresh command-line processes otherwise abort in CGS_REQUIRE_INIT.
        if not pooled(Quartz.CGMainDisplayID):
            raise RuntimeError("--fast2 requires a logged-in Mac graphical session with a display")
        import CoreMedia  # Register CMSampleBuffer types before importing SCK.
        import ScreenCaptureKit
    except ImportError as exc:
        raise RuntimeError(
            "ScreenCaptureKit wrappers are missing. Install the updated project with "
            "'python3 -m pip install -e .' and grant Screen Recording permission."
        ) from exc
    return SimpleNamespace(objc=objc, F=Foundation, Q=Quartz, CM=CoreMedia, SCK=ScreenCaptureKit)


def output_class(api):
    global _OUTPUT_CLASS
    if _OUTPUT_CLASS is None:
        class SimmerStreamOutput(api.F.NSObject, protocols=[
            api.objc.protocolNamed("SCStreamOutput"), api.objc.protocolNamed("SCStreamDelegate"),
        ]):
            def stream_didOutputSampleBuffer_ofType_(self, stream, sample, kind):
                owner = self.owner
                if owner is not None:
                    try:
                        pooled(owner.on_sample, sample, kind)
                    except BaseException as exc:
                        owner.fail("ScreenCaptureKit output: " + repr(exc))

            def stream_didStopWithError_(self, stream, error):
                owner = self.owner
                if owner is not None and not owner.stopping:
                    owner.fail("ScreenCaptureKit stopped: " + str(error))

        _OUTPUT_CLASS = SimmerStreamOutput
    return _OUTPUT_CLASS


async def completed_call(invoke, *, result=False, timeout=10):
    loop = asyncio.get_running_loop()
    future = loop.create_future()

    def completed(*values):
        def deliver():
            if future.done():
                return
            if values[-1] is not None:
                future.set_exception(RuntimeError(str(values[-1])))
            else:
                future.set_result(values[0] if result else None)
        try:
            loop.call_soon_threadsafe(deliver)
        except RuntimeError:
            pass  # A late native completion after shutdown.

    pooled(invoke, completed)
    return await asyncio.wait_for(future, timeout)


class WindowCapture:
    """One latest CVPixelBuffer, one in-flight encoder reference, one JPEG cache."""

    def __init__(self, api, window):
        self.api = api
        self.target = (int(window["wid"]), int(window["width"]), int(window["height"]))
        self.lock = threading.Lock()
        self.encode_lock = threading.Lock()
        self.latest = self.cache = self.cache_key = self.context = None
        self.stream = self.output = self.config = self.filter = None
        self.accepting = self.stopping = self.start_attempted = False
        self.version = 0
        self.started_at = None
        self.error = None

    def fail(self, error):
        with self.lock:
            self.error = str(error)[:2000]
            self.latest = self.cache = self.cache_key = None

    def on_sample(self, sample, kind):
        cm, q, sck = self.api.CM, self.api.Q, self.api.SCK
        with self.lock:
            if not self.accepting:
                return
        if kind != sck.SCStreamOutputTypeScreen:
            return
        if not cm.CMSampleBufferIsValid(sample) or not cm.CMSampleBufferDataIsReady(sample):
            return
        attachments = cm.CMSampleBufferGetSampleAttachmentsArray(sample, False)
        if not attachments or sck.SCStreamFrameInfoStatus not in attachments[0]:
            raise RuntimeError("ScreenCaptureKit sample has no frame status")
        status = int(attachments[0][sck.SCStreamFrameInfoStatus])
        if status != sck.SCFrameStatusComplete:
            if status in (sck.SCFrameStatusBlank, sck.SCFrameStatusSuspended):
                with self.lock:
                    self.latest = self.cache = self.cache_key = None
            return  # Idle retains the last valid frame, including on static screens.
        image = cm.CMSampleBufferGetImageBuffer(sample)
        if image is None or q.CVPixelBufferGetPixelFormatType(image) != q.kCVPixelFormatType_32BGRA:
            raise RuntimeError("ScreenCaptureKit did not deliver a BGRA pixel buffer")
        with self.lock:
            if self.accepting:
                self.latest = image
                self.version += 1

    def configure(self, content):
        sck, q, cm = self.api.SCK, self.api.Q, self.api.CM
        wid, width, height = self.target
        window = next((w for w in content.windows() if int(w.windowID()) == wid), None)
        if window is None:
            raise RuntimeError("Window is not shareable; check Screen Recording permission and window visibility")
        self.filter = sck.SCContentFilter.alloc().initWithDesktopIndependentWindow_(window)
        self.config = sck.SCStreamConfiguration.alloc().init()
        self.config.setWidth_(width)
        self.config.setHeight_(height)
        self.config.setMinimumFrameInterval_(cm.CMTimeMake(1, 60))
        self.config.setPixelFormat_(q.kCVPixelFormatType_32BGRA)
        self.config.setQueueDepth_(3)
        self.config.setShowsCursor_(False)
        self.config.setCapturesAudio_(False)
        self.config.setIgnoreShadowsSingleWindow_(True)
        self.config.setShouldBeOpaque_(True)
        self.output = output_class(self.api).alloc().init()
        self.output.owner = self
        self.stream = sck.SCStream.alloc().initWithFilter_configuration_delegate_(
            self.filter, self.config, self.output,
        )
        ok, error = self.stream.addStreamOutput_type_sampleHandlerQueue_error_(
            self.output, sck.SCStreamOutputTypeScreen, None, None,
        )
        if not ok or error is not None:
            raise RuntimeError("Cannot register ScreenCaptureKit output: " + str(error))

    async def start(self):
        content = await completed_call(
            lambda cb: self.api.SCK.SCShareableContent
            .getShareableContentExcludingDesktopWindows_onScreenWindowsOnly_completionHandler_(True, True, cb),
            result=True,
        )
        pooled(self.configure, content)
        with self.lock:
            self.accepting = True
        self.start_attempted = True
        await completed_call(self.stream.startCaptureWithCompletionHandler_)
        self.started_at = time.monotonic()

    def capture(self, quality):
        with self.encode_lock:
            try:
                return pooled(self._encode, max(0, min(100, int(quality))))
            except Exception as exc:
                self.fail("ScreenCaptureKit JPEG: " + str(exc))
                return None

    def _encode(self, quality):
        with self.lock:
            if self.stopping or self.error or self.latest is None:
                return None
            image, version = self.latest, self.version
            key = (version, quality)
            if self.cache_key == key:
                return self.cache
        q, f = self.api.Q, self.api.F
        if self.context is None:
            options = f.NSDictionary.dictionaryWithObject_forKey_(
                f.NSNumber.numberWithBool_(False), q.kCIContextCacheIntermediates,
            )
            self.context = q.CIContext.contextWithOptions_(options)
        ci = q.CIImage.imageWithCVPixelBuffer_(image)
        cg = self.context.createCGImage_fromRect_format_colorSpace_deferred_(
            ci, ci.extent(), q.kCIFormatRGBA8, None, False,
        )
        if cg is None:
            raise RuntimeError("Core Image failed to materialize the captured window")
        data = encode_jpeg(cg, quality)
        if not data:
            raise RuntimeError("JPEG encoder produced no frame")
        with self.lock:
            if not self.stopping and not self.error and self.latest is not None:
                self.cache, self.cache_key = data, key
        return data

    def _clear(self):
        # A worker may still be encoding when disconnect/resize initiates close.
        with self.encode_lock:
            with self.lock:
                self.latest = self.cache = self.cache_key = None
            self.context = None

    def _detach(self):
        try:
            if self.stream is not None and self.output is not None:
                ok, error = self.stream.removeStreamOutput_type_error_(
                    self.output, self.api.SCK.SCStreamOutputTypeScreen, None,
                )
                if not ok or error is not None:
                    raise RuntimeError("Cannot remove ScreenCaptureKit output: " + str(error))
        finally:
            if self.output is not None:
                self.output.owner = None
            self.stream = self.output = self.config = self.filter = None

    async def close(self):
        with self.lock:
            self.stopping = True
            self.accepting = False
        try:
            if self.start_attempted and self.stream is not None:
                await completed_call(self.stream.stopCaptureWithCompletionHandler_)
        finally:
            try:
                pooled(self._detach)
            finally:
                await asyncio.to_thread(pooled, self._clear)


@dataclass
class _DeviceState:
    requested: float
    stream: WindowCapture | None = None
    task: asyncio.Task | None = None
    message: str | None = None


class CaptureService:
    """Main-loop stream management; capture workers never wait for native startup.

    One watcher per recently requested UDID. Resize/replaced-window recovery and
    a five-second idle timeout cover rotation, pause, disconnect and reconnect
    without adding queues of frames or per-poll asyncio tasks.
    """

    def __init__(self):
        self.api = self.loop = self.pump = None
        self.lock = threading.Lock()
        self.devices: dict[str, _DeviceState] = {}
        self.stopping = False
        self.fatal_error = None

    async def startup(self):
        if self.loop is not None:
            return
        self.api = load_api()
        self.loop = asyncio.get_running_loop()
        self.stopping = False
        self.fatal_error = None
        self.pump = asyncio.create_task(self._pump())

    def _pump_once(self):
        f = self.api.F
        f.NSRunLoop.currentRunLoop().runMode_beforeDate_(
            f.NSDefaultRunLoopMode, f.NSDate.dateWithTimeIntervalSinceNow_(0),
        )

    async def _pump(self):
        try:
            while True:
                pooled(self._pump_once)
                await asyncio.sleep(0.01)
        except Exception as exc:
            self.fatal_error = str(exc)
            log.error("[fast2] Cocoa run loop failed: %s", exc)

    def capture(self, udid, quality, find_window):
        with self.lock:
            if self.loop is None or self.stopping or self.fatal_error:
                return None
            state = self.devices.get(udid)
            if state is None:
                state = _DeviceState(time.monotonic())
                self.devices[udid] = state
                self.loop.call_soon_threadsafe(self._launch, udid, state, find_window)
            else:
                state.requested = time.monotonic()
            stream = state.stream
        return stream.capture(quality) if stream is not None else None

    def _launch(self, udid, state, find_window):
        if not self.stopping:
            state.task = asyncio.create_task(self._watch(udid, state, find_window))

    def _message(self, udid, state, message):
        if state.message != message:
            log.warning("[fast2 %s] %s", udid, message)
            state.message = message

    async def _close_stream(self, state):
        with self.lock:
            stream, state.stream = state.stream, None
        if stream is not None:
            try:
                await stream.close()
            except Exception as exc:
                # Never accumulate replacement native streams after a failed stop.
                self.fatal_error = str(exc)
                log.error("[fast2] Stream cleanup failed; restart simmer: %s", exc)

    async def _watch(self, udid, state, find_window):
        try:
            while not self.stopping and not self.fatal_error:
                with self.lock:
                    idle = time.monotonic() - state.requested > 5
                    stream = state.stream
                if idle:
                    break
                try:
                    window = await asyncio.to_thread(pooled, find_window, udid)
                    target = None if window is None else (
                        int(window["wid"]), int(window["width"]), int(window["height"]),
                    )
                    if (stream is not None and stream.started_at is not None and stream.version == 0
                            and time.monotonic() - stream.started_at > 15):
                        stream.fail("No complete frame for 15 seconds; check Screen Recording permission")
                    if stream is not None and (stream.target != target or stream.error):
                        if stream.error:
                            self._message(udid, state, stream.error)
                        await self._close_stream(state)
                        stream = None
                    if target is None:
                        self._message(udid, state, "Waiting for a visible simulator/emulator window")
                    elif stream is None and not self.fatal_error:
                        stream = WindowCapture(self.api, window)
                        # Publish for finally cleanup, including cancelled startup.
                        with self.lock:
                            state.stream = stream
                        try:
                            await stream.start()
                        except BaseException:
                            await self._close_stream(state)
                            raise
                        state.message = None
                except Exception as exc:
                    self._message(udid, state, str(exc))
                    await asyncio.sleep(2)
                await asyncio.sleep(0.5)
        finally:
            await self._close_stream(state)
            with self.lock:
                if self.devices.get(udid) is state:
                    self.devices.pop(udid, None)

    async def shutdown(self):
        with self.lock:
            self.stopping = True
            tasks = [state.task for state in self.devices.values() if state.task is not None]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        if self.pump is not None:
            self.pump.cancel()
            await asyncio.gather(self.pump, return_exceptions=True)
        with self.lock:
            self.devices.clear()
            self.loop = self.pump = None
