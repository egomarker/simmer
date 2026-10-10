"""The fast2 Android image crop and Quartz touch mapping must share bounds."""
from __future__ import annotations

import importlib
import sys
from types import SimpleNamespace

import pytest


@pytest.fixture
def input_module(monkeypatch):
    # Quartz is macOS-only, but normalized-coordinate math is portable.
    fake_quartz = SimpleNamespace(
        kCGEventLeftMouseDown=1,
        kCGEventLeftMouseDragged=2,
        kCGEventLeftMouseUp=3,
    )
    monkeypatch.setitem(sys.modules, "Quartz", fake_quartz)
    module = importlib.import_module("simmer.backend_android_input_quartz")
    module._TOUCHES.clear()
    yield module
    module._TOUCHES.clear()


def test_fast2_crop_maps_first_and_last_visible_pixel(input_module):
    rect = {"x": 50, "y": 90, "width": 300, "height": 700}
    assert input_module._point(rect, 0, 0, 28) == (50, 118)
    assert input_module._point(rect, 1, 1, 28) == (350, 790)
    assert input_module._point(rect, 0.5, 0.5, 28) == (200, 454)


def test_regular_fast_mode_retains_full_window_mapping(input_module):
    rect = {"x": 50, "y": 90, "width": 300, "height": 700}
    assert input_module._point(rect, 0, 0) == (50, 90)
    assert input_module._point(rect, 1, 1) == (350, 790)


def test_fast2_gesture_uses_cropped_bounds_on_down_move_up(input_module, monkeypatch):
    rect = {"x": 50, "y": 90, "width": 300, "height": 700}
    events = []
    monkeypatch.setattr(input_module, "_mouse", lambda kind, x, y: events.append((kind, x, y)))

    class FakeInput(input_module.QuartzInputMixin):
        _input_crop_top = 28

        def _input_rect(self, udid):
            return rect

    backend = FakeInput()
    backend.touch_down("emulator-5554", 0.5, 0)
    backend.touch_move("emulator-5554", 0.5, 0.5)
    backend.touch_up("emulator-5554", 0.5, 1)

    assert events == [(1, 200, 118), (2, 200, 454), (2, 200, 790), (3, 200, 790)]


@pytest.mark.parametrize("configured_height", [None, 36])
def test_fast2_capture_and_input_share_titlebar_height(monkeypatch, input_module, configured_height):
    """The default constant and a future setting must affect both paths."""
    from simmer.backend_sck import AndroidScreenCaptureBackend
    from simmer.constants import ANDROID_FAST2_TITLEBAR_HEIGHT
    import simmer.backend_android_quartz as quartz_module

    rect = {"x": 50, "y": 90, "width": 300, "height": 700}
    monkeypatch.setattr(quartz_module.AndroidQuartzBackend, "_input_rect", lambda self, udid: rect)
    monkeypatch.setattr(quartz_module.AndroidQuartzBackend, "_window_id", lambda self, udid: 42)

    options = {} if configured_height is None else {"titlebar_height": configured_height}
    backend = AndroidScreenCaptureBackend(service=SimpleNamespace(), **options)
    height = ANDROID_FAST2_TITLEBAR_HEIGHT if configured_height is None else configured_height

    assert backend._input._input_crop_top == height
    assert backend._find_window("emulator-5554")["crop"] == (0, height, 300, 700 - height)
    assert input_module._point(rect, 0.5, 0, backend._input._input_crop_top) == (200, 90 + height)


def test_headless_adb_fallback_keeps_device_normalized_coordinates(input_module, monkeypatch):
    adb_taps = []
    monkeypatch.setattr(
        input_module, "_adb",
        lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout=b"Physical size: 1080x2400"),
    )

    class FakeAdbBase:
        def tap(self, udid, nx, ny, dev_w, dev_h):
            adb_taps.append((udid, nx, ny, dev_w, dev_h))

    class FakeInput(input_module.QuartzInputMixin, FakeAdbBase):
        _input_crop_top = 28

        def _input_rect(self, udid):
            return None

    backend = FakeInput()
    backend.touch_down("emulator-5554", 0.3, 0.2)
    backend.touch_up("emulator-5554", 0.3, 0.2)
    assert adb_taps == [("emulator-5554", 0.3, 0.2, 1080, 2400)]
