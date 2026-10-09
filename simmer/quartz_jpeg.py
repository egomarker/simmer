"""Shared Quartz JPEG encoding. Call inside the capture worker's autorelease pool."""
from __future__ import annotations

from functools import lru_cache


@lru_cache(maxsize=101)
def _native_jpeg_properties(quality: int):
    """Keep bounded, immutable native options instead of bridging a dict per frame."""
    import AppKit

    return AppKit.NSDictionary.dictionaryWithObject_forKey_(
        AppKit.NSNumber.numberWithDouble_(quality / 100.0),
        AppKit.NSImageCompressionFactor,
    )


def encode_jpeg(image: object, quality: int) -> bytes | None:
    import AppKit
    import CoreFoundation

    quality = max(0, min(100, int(quality)))
    bitmap = AppKit.NSBitmapImageRep.alloc().initWithCGImage_(image)
    if bitmap is None:
        return None
    data = bitmap.representationUsingType_properties_(
        AppKit.NSBitmapImageFileTypeJPEG,
        _native_jpeg_properties(quality),
    )
    if data is None:
        return None
    # Avoid PyObjC varlist.as_buffer(), which retains its exporter in affected versions.
    length = CoreFoundation.CFDataGetLength(data)
    return CoreFoundation.CFDataGetBytes(data, (0, length), None)
