import io

import pytest
from PIL import Image


def encode(img: Image.Image, fmt: str, **kw) -> bytes:
    buf = io.BytesIO()
    img.save(buf, fmt, **kw)
    return buf.getvalue()


def heic_bytes(size=(400, 300), colour="orange") -> bytes:
    import pillow_heif

    buf = io.BytesIO()
    pillow_heif.from_pillow(Image.new("RGB", size, colour)).save(buf, format="HEIF")
    return buf.getvalue()


@pytest.fixture
def jpeg_bytes() -> bytes:
    return encode(Image.new("RGB", (3000, 2000), "red"), "JPEG")


@pytest.fixture
def png_bytes() -> bytes:
    return encode(Image.new("RGB", (800, 600), "blue"), "PNG")


@pytest.fixture
def heic() -> bytes:
    return heic_bytes()
