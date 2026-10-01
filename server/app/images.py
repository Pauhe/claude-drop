"""Image normalisation. Pure functions over bytes — no I/O, no framework.

Everything Claude can actually read is decided here. Claude supports only
JPEG, PNG, GIF and WebP, so HEIC from an iPhone must be converted or it
arrives unreadable.
"""

from __future__ import annotations

import io
from dataclasses import dataclass

import pillow_heif
from PIL import Image, ImageOps

pillow_heif.register_heif_opener()

MAX_EDGE = 2000
# The API's own ceiling, which full_jpg may use but view variants may not.
MAX_API_EDGE = 8000
JPEG_QUALITY = 95
MAX_VARIANT_BYTES = 10 * 1024 * 1024

# An RGBA buffer at 40 Mpx is ~160 MB; the container is limited to 2 GB and
# allows two concurrent decodes. Resizing after decoding does not help — the
# allocation has already happened.
MAX_SOURCE_PIXELS = 40_000_000
Image.MAX_IMAGE_PIXELS = None  # we enforce our own limit explicitly

READABLE = {"image/jpeg", "image/png", "image/gif", "image/webp"}
LOSSLESS_FORMATS = {"PNG", "BMP", "TIFF"}

HEIF_BRANDS = {
    b"heic", b"heix", b"hevc", b"hevx", b"heim",
    b"heis", b"hevm", b"hevs", b"mif1", b"msf1",
}

_MAGIC: tuple[tuple[bytes, str], ...] = (
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"GIF87a", "image/gif"),
    (b"GIF89a", "image/gif"),
    (b"BM", "image/bmp"),
    (b"II*\x00", "image/tiff"),
    (b"MM\x00*", "image/tiff"),
    (b"%PDF-", "application/pdf"),
)


@dataclass(frozen=True)
class Variant:
    kind: str
    data: bytes
    width: int
    height: int


@dataclass(frozen=True)
class Normalised:
    kind: str
    media_type: str
    variants: list[Variant]
    width: int | None
    height: int | None
    error: str | None


def detect_media_type(data: bytes) -> str | None:
    """Identify a format from its bytes. There is no filename parameter on
    purpose: an extension is never evidence."""
    for magic, media_type in _MAGIC:
        if data[: len(magic)] == magic:
            return media_type
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    if data[4:8] == b"ftyp":
        # The major brand sits at 8:12; compatible brands follow from 16.
        if data[8:12] in HEIF_BRANDS:
            return "image/heic"
        compatible = data[16:64]
        if any(brand in compatible for brand in HEIF_BRANDS):
            return "image/heic"
    return None


def directly_usable(media_type: str, width: int, height: int, size: int) -> bool:
    """Whether the uploaded bytes can go to the API untouched.

    Format alone is not enough: a 12 MB PNG is a supported format the API
    still rejects, and so needs a full_jpg like a HEIC does.
    """
    return (media_type in READABLE
            and size <= MAX_VARIANT_BYTES
            and max(width, height) <= MAX_API_EDGE)


def _fit(img: Image.Image, max_edge: int) -> Image.Image:
    longest = max(img.width, img.height)
    if longest <= max_edge:
        return img
    scale = max_edge / longest
    return img.resize(
        (max(1, round(img.width * scale)), max(1, round(img.height * scale))),
        Image.LANCZOS,
    )


def _strip(img: Image.Image) -> Image.Image:
    """Return a copy carrying no metadata.

    Pillow propagates img.info through a save, so a PNG variant keeps EXIF
    and text chunks unless they are dropped deliberately.

    frombytes/tobytes, never putdata(list(getdata())): the latter materialises
    one Python tuple per pixel, which for a 40 Mpx image is several gigabytes
    — more than the container has — before any encoder allocates anything.
    """
    return Image.frombytes(img.mode, img.size, img.tobytes())


def _encode_jpeg(img: Image.Image, quality: int = JPEG_QUALITY) -> bytes:
    flat = img
    if flat.mode in ("RGBA", "LA"):
        canvas = Image.new("RGB", flat.size, (255, 255, 255))
        canvas.paste(flat, mask=flat.split()[-1])
        flat = canvas
    elif flat.mode != "RGB":
        flat = flat.convert("RGB")
    buf = io.BytesIO()
    _strip(flat).save(buf, "JPEG", quality=quality, optimize=True)
    return buf.getvalue()


def _encode_png(img: Image.Image) -> bytes:
    keep = img if img.mode in ("RGB", "RGBA") else img.convert("RGBA")
    buf = io.BytesIO()
    _strip(keep).save(buf, "PNG", optimize=True)
    return buf.getvalue()


def _bounded_jpeg(img: Image.Image, max_edge: int = MAX_EDGE
                  ) -> tuple[bytes, int, int]:
    """Encode within the 10 MB the API accepts.

    Returns the bytes AND the dimensions they actually carry: dropping
    quality is tried first, and only then resolution — but whichever gives
    way, the caller must record what it really got. A Variant whose width and
    height describe a size its bytes do not have is a lie that the CLI and
    the UI would both repeat.
    """
    candidate = _fit(img, max_edge)
    while True:
        for quality in (JPEG_QUALITY, 80, 65):
            data = _encode_jpeg(candidate, quality)
            if len(data) <= MAX_VARIANT_BYTES:
                return data, candidate.width, candidate.height
        if max(candidate.width, candidate.height) <= 400:
            # Nothing sensible is left to give; ship the smallest attempt and
            # let the caller's recorded dimensions match it.
            return data, candidate.width, candidate.height
        candidate = _fit(candidate, max(candidate.width, candidate.height) // 2)


def _build_variants(img: Image.Image, source_format: str, media_type: str,
                    original_size: int) -> list[Variant]:
    fitted = _fit(img, MAX_EDGE)
    variants: list[Variant] = []

    if source_format in LOSSLESS_FORMATS:
        png = _encode_png(fitted)
        if len(png) <= MAX_VARIANT_BYTES:
            variants.append(Variant("view_png", png, fitted.width, fitted.height))

    variants.append(Variant("view_jpg", *_bounded_jpeg(fitted)))

    if not directly_usable(media_type, img.width, img.height, original_size):
        variants.append(Variant("full_jpg", *_bounded_jpeg(img, MAX_API_EDGE)))
    return variants


def normalise(data: bytes) -> Normalised:
    media_type = detect_media_type(data)
    if media_type is None or not media_type.startswith("image/"):
        return Normalised("raw", media_type or "application/octet-stream",
                          [], None, None, None)

    try:
        with Image.open(io.BytesIO(data)) as opened:
            if opened.width * opened.height > MAX_SOURCE_PIXELS:
                raise ValueError(
                    f"source is {opened.width}x{opened.height}, over the "
                    f"{MAX_SOURCE_PIXELS} pixel limit"
                )
            source_format = opened.format or ""
            img = ImageOps.exif_transpose(opened)
            img.load()
            original = (img.width, img.height)
            # convert("RGBA") first: a direct convert("RGB") from a palette
            # image discards palette transparency before either encoder sees it.
            if img.mode == "P":
                img = (img.convert("RGBA") if "transparency" in img.info
                       else img.convert("RGB"))
            elif img.mode == "CMYK":
                img = img.convert("RGB")
            variants = _build_variants(img, source_format, media_type, len(data))
    except Exception as exc:  # noqa: BLE001 — every decoder failure is one outcome
        return Normalised("failed", media_type, [], None, None,
                          f"{type(exc).__name__}: {exc}")

    return Normalised("image", media_type, variants, original[0], original[1], None)
