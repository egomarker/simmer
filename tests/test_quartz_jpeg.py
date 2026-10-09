"""Portable regression tests for Quartz JPEG ownership and backend integration."""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from simmer import quartz_jpeg


class NativeDictionary:
    def __init__(self, number, key):
        self.number = number
        self.key = key


@pytest.fixture
def frameworks(monkeypatch):
    bitmap = SimpleNamespace(representationUsingType_properties_=Mock(return_value=b"jpeg-data"))
    allocator = SimpleNamespace(initWithCGImage_=Mock(return_value=bitmap))
    appkit = SimpleNamespace(
        NSImageCompressionFactor="compression-factor",
        NSBitmapImageFileTypeJPEG=3,
        NSBitmapImageRep=SimpleNamespace(alloc=Mock(return_value=allocator)),
        NSNumber=SimpleNamespace(numberWithDouble_=Mock(side_effect=lambda n: ("native-number", n))),
        NSDictionary=SimpleNamespace(dictionaryWithObject_forKey_=Mock(side_effect=NativeDictionary)),
        NSAutoreleasePool=SimpleNamespace(alloc=Mock(return_value=SimpleNamespace(init=lambda: object()))),
    )
    cf = SimpleNamespace(
        CFDataGetLength=Mock(return_value=9),
        CFDataGetBytes=Mock(return_value=b"jpeg-data"),
        CFDataGetBytePtr=Mock(side_effect=AssertionError("Do not use the leaking pointer/memoryview path")),
    )
    quartz = SimpleNamespace(
        CGRectNull=None,
        kCGWindowListOptionIncludingWindow=1,
        kCGWindowImageBoundsIgnoreFraming=2,
        kCGWindowImageNominalResolution=4,
        CGWindowListCreateImage=Mock(return_value=object()),
    )
    monkeypatch.setitem(sys.modules, "AppKit", appkit)
    monkeypatch.setitem(sys.modules, "CoreFoundation", cf)
    monkeypatch.setitem(sys.modules, "Quartz", quartz)
    quartz_jpeg._native_jpeg_properties.cache_clear()
    yield SimpleNamespace(appkit=appkit, cf=cf, quartz=quartz, bitmap=bitmap, allocator=allocator)
    quartz_jpeg._native_jpeg_properties.cache_clear()


def test_native_options_and_direct_byte_copy(frameworks):
    image = object()
    assert quartz_jpeg.encode_jpeg(image, 20) == b"jpeg-data"
    frameworks.allocator.initWithCGImage_.assert_called_once_with(image)
    jpeg_type, options = frameworks.bitmap.representationUsingType_properties_.call_args.args
    assert jpeg_type == 3
    assert isinstance(options, NativeDictionary)
    assert options.number == ("native-number", 0.2)
    assert options.key == "compression-factor"
    frameworks.cf.CFDataGetBytes.assert_called_once_with(b"jpeg-data", (0, 9), None)
    frameworks.cf.CFDataGetBytePtr.assert_not_called()


def test_reuses_native_options_for_same_quality(frameworks):
    for _ in range(100):
        assert quartz_jpeg.encode_jpeg(object(), 20) == b"jpeg-data"
    frameworks.appkit.NSDictionary.dictionaryWithObject_forKey_.assert_called_once()
    options = [call.args[1] for call in frameworks.bitmap.representationUsingType_properties_.call_args_list]
    assert all(value is options[0] for value in options)


def test_separate_quality_values(frameworks):
    quartz_jpeg.encode_jpeg(object(), 20)
    quartz_jpeg.encode_jpeg(object(), 70)
    assert frameworks.appkit.NSDictionary.dictionaryWithObject_forKey_.call_count == 2
    assert quartz_jpeg._native_jpeg_properties.cache_info().currsize == 2


@pytest.mark.parametrize(("quality", "expected"), [(-10, 0.0), (0, 0.0), (20, 0.2), (100, 1.0), (200, 1.0)])
def test_quality_clamping(frameworks, quality, expected):
    quartz_jpeg.encode_jpeg(object(), quality)
    frameworks.appkit.NSNumber.numberWithDouble_.assert_called_once_with(expected)


def test_cache_is_bounded(frameworks):
    for quality in range(-10, 111):
        quartz_jpeg.encode_jpeg(object(), quality)
    assert quartz_jpeg._native_jpeg_properties.cache_info().currsize == 101
    assert frameworks.appkit.NSDictionary.dictionaryWithObject_forKey_.call_count == 101


def test_missing_bitmap(frameworks):
    frameworks.allocator.initWithCGImage_.return_value = None
    assert quartz_jpeg.encode_jpeg(object(), 20) is None
    frameworks.bitmap.representationUsingType_properties_.assert_not_called()
    frameworks.cf.CFDataGetBytes.assert_not_called()


def test_missing_jpeg(frameworks):
    frameworks.bitmap.representationUsingType_properties_.return_value = None
    assert quartz_jpeg.encode_jpeg(object(), 20) is None
    frameworks.cf.CFDataGetBytes.assert_not_called()


def test_copy_error_propagates_to_backend(frameworks):
    frameworks.cf.CFDataGetBytes.side_effect = RuntimeError("copy failure")
    with pytest.raises(RuntimeError, match="copy failure"):
        quartz_jpeg.encode_jpeg(object(), 20)


def load_backend(stem, monkeypatch):
    # Load a separate module object; do not replace a real backend in sys.modules.
    # Touch input is outside this capture test, so avoid importing its macOS mixin.
    if stem == "backend_android_quartz":
        monkeypatch.setitem(
            sys.modules, "simmer.backend_android_input_quartz",
            SimpleNamespace(QuartzInputMixin=type("QuartzInputMixin", (), {})),
        )
    path = Path(quartz_jpeg.__file__).with_name(stem + ".py")
    spec = importlib.util.spec_from_file_location("simmer._test_" + stem, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_ios_uses_shared_encoder(frameworks, monkeypatch):
    backend = load_backend("backend_quartz", monkeypatch)
    monkeypatch.setattr(backend, "_find_window", lambda _: {"wid": 17})
    assert backend.capture("SIM", 20) == b"jpeg-data"
    frameworks.cf.CFDataGetBytes.assert_called_once()


@pytest.mark.parametrize("missing", ["window", "image"])
def test_ios_missing_capture(frameworks, monkeypatch, missing):
    backend = load_backend("backend_quartz", monkeypatch)
    monkeypatch.setattr(backend, "_find_window", lambda _: None if missing == "window" else {"wid": 17})
    frameworks.quartz.CGWindowListCreateImage.return_value = None
    assert backend.capture("SIM", 20) is None
    frameworks.cf.CFDataGetBytes.assert_not_called()


def android_backend(monkeypatch):
    module = load_backend("backend_android_quartz", monkeypatch)
    fallback = Mock(return_value=b"adb-frame")
    monkeypatch.setattr(module.AdbBackend, "capture", fallback)
    backend = module.AndroidQuartzBackend()
    monkeypatch.setattr(backend, "_window_id", lambda _: 17)
    return backend, fallback


def test_android_uses_shared_encoder(frameworks, monkeypatch):
    backend, fallback = android_backend(monkeypatch)
    assert backend.capture("emulator-5554", 20) == b"jpeg-data"
    fallback.assert_not_called()
    frameworks.cf.CFDataGetBytes.assert_called_once()


@pytest.mark.parametrize("failure", ["window", "image", "jpeg", "copy"])
def test_android_preserves_adb_fallback(frameworks, monkeypatch, failure):
    backend, fallback = android_backend(monkeypatch)
    backend._window_ids["emulator-5554"] = (17, 0)
    if failure == "window":
        monkeypatch.setattr(backend, "_window_id", lambda _: None)
    elif failure == "image":
        frameworks.quartz.CGWindowListCreateImage.return_value = None
    elif failure == "jpeg":
        frameworks.bitmap.representationUsingType_properties_.return_value = None
    else:
        frameworks.cf.CFDataGetBytes.side_effect = RuntimeError("copy failure")
    assert backend.capture("emulator-5554", 20) == b"adb-frame"
    fallback.assert_called_once_with("emulator-5554", 20)
    if failure == "image":
        assert "emulator-5554" not in backend._window_ids
