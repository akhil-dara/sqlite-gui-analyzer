"""Image previews of BLOBs within a pixel budget.

An image declares its own width and height, and decoding it takes about four bytes per pixel:
a few hundred KB of PNG can ask for gigabytes. Every preview (the BLOB inspector, the record
view's thumbnails) first reads the size from the image header and draws nothing larger than
limits 'preview_pixels'; the caller shows the reason instead.
"""

import io
import struct
import warnings

from constants import HAS_PIL
from engine import limits

if HAS_PIL:
    from constants import PILImage


class PreviewRefused(Exception):
    """The image is not previewed; str() says why in plain words."""


def header_size(data):
    """(width, height) from a PNG or GIF header, or None (other formats need Pillow)."""
    if data[:8] == b"\x89PNG\r\n\x1a\n" and len(data) >= 24 and data[12:16] == b"IHDR":
        return struct.unpack(">II", data[16:24])
    if data[:4] == b"GIF8" and len(data) >= 10:
        return struct.unpack("<HH", data[6:10])
    return None


def check_size(width, height):
    """Raise PreviewRefused when width x height is more than limits 'preview_pixels'."""
    most = limits.get("preview_pixels")
    if width <= 0 or height <= 0:
        raise PreviewRefused("not previewed: the image declares a size of %d x %d"
                             % (width, height))
    if width * height > most:
        raise PreviewRefused("not previewed: %s x %s pixels is more than the limit "
                             "preview_pixels (%s; Limits…)"
                             % (format(width, ","), format(height, ","), format(most, ",")))


def open_image(data, draft=None):
    """A loaded Pillow image of data, whose size was checked first (header only). draft:
    (w, h) the image may be decoded at a reduced size (JPEG). Raises PreviewRefused, or
    Pillow's own errors for data it cannot read."""
    if not HAS_PIL:
        raise PreviewRefused("Pillow is not installed")
    with warnings.catch_warnings():
        # Pillow warns about a large image when it reads its header; the size check below
        # decides (and says why) instead
        warnings.simplefilter("ignore")
        im = PILImage.open(io.BytesIO(data))
    w, h = im.size
    check_size(w, h)
    if draft is not None and getattr(im, "format", None) == "JPEG":
        try:
            im.draft("RGB", draft)
        except Exception:       # noqa: BLE001 - draft is only a speed-up
            pass
    try:
        im.load()
    except MemoryError:
        raise PreviewRefused("not previewed: not enough memory to decode %d x %d pixels"
                             % (w, h))
    return im


def thumbnail(data, size=(80, 80)):
    """A Pillow thumbnail of data no larger than size (raises as open_image)."""
    im = open_image(data, draft=size)
    im.thumbnail(size)
    return im


def zoomed_size(width, height, zoom):
    """(w, h) of the image at `zoom`, reduced so that it stays within 'preview_pixels'."""
    w, h = max(1, int(width * zoom)), max(1, int(height * zoom))
    most = limits.get("preview_pixels")
    if w * h > most:
        scale = (most / float(w * h)) ** 0.5
        w, h = max(1, int(w * scale)), max(1, int(h * scale))
    return w, h
