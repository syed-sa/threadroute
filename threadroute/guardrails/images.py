"""Safe handling of user-uploaded images: validate, strip metadata (EXIF/GPS), downscale, re-encode.

Pillow decodes the image (the content-type header is never trusted), rejects anything that is not a real
PNG/JPEG/WEBP, caps pixel count (decompression bombs), removes all metadata by re-encoding, and shrinks the
longest side - which also cuts the image tokens sent to the model.
"""

from __future__ import annotations

import io

from PIL import Image, ImageOps

MAX_PIXELS = 25_000_000
MAX_SIDE = 1568
ALLOWED_FORMATS = {"PNG", "JPEG", "WEBP"}


class UnsafeImage(ValueError):
    pass


def sanitize_image(data: bytes) -> tuple[bytes, str]:
    Image.MAX_IMAGE_PIXELS = MAX_PIXELS
    try:
        probe = Image.open(io.BytesIO(data))
        fmt = (probe.format or "").upper()
        probe.verify()  # structural check
        img = Image.open(io.BytesIO(data))
        if fmt not in ALLOWED_FORMATS:
            raise UnsafeImage("Only real PNG, JPEG or WEBP images are accepted.")
        if img.width * img.height > MAX_PIXELS:
            raise UnsafeImage("Image dimensions are too large.")
        img = ImageOps.exif_transpose(img)  # apply orientation, then drop metadata below
        img.load()
    except UnsafeImage:
        raise
    except Exception as exc:  # noqa: BLE001 - any decode problem means "not a usable image"
        raise UnsafeImage("That file is not a valid image.") from exc
    if max(img.size) > MAX_SIDE:
        img.thumbnail((MAX_SIDE, MAX_SIDE))
    rgb = img.convert("RGB")
    out = io.BytesIO()
    rgb.save(out, format="JPEG", quality=88, optimize=True)  # fresh encode: no EXIF, GPS, ICC or comments
    return out.getvalue(), "image/jpeg"
