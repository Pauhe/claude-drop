import io

from PIL import Image

from app import images
from tests.conftest import encode, heic_bytes


def kinds(data: bytes) -> list[str]:
    return [v.kind for v in images.normalise(data).variants]


def variant(data: bytes, kind: str) -> images.Variant:
    return next(v for v in images.normalise(data).variants if v.kind == kind)


def test_png_gets_a_lossless_view_first(png_bytes):
    assert kinds(png_bytes) == ["view_png", "view_jpg"]


def test_jpeg_gets_no_full_variant_because_the_original_already_is_one(jpeg_bytes):
    assert "full_jpg" not in kinds(jpeg_bytes)


def test_png_gets_no_full_variant_either(png_bytes):
    assert "full_jpg" not in kinds(png_bytes)


def test_heic_gets_a_full_resolution_readable_variant():
    data = heic_bytes(size=(3000, 2000))
    produced = kinds(data)
    assert "full_jpg" in produced
    full = variant(data, "full_jpg")
    assert (full.width, full.height) == (3000, 2000)
    assert images.detect_media_type(full.data) == "image/jpeg"


def test_heic_view_is_still_capped():
    data = heic_bytes(size=(3000, 2000))
    view = variant(data, "view_jpg")
    assert max(view.width, view.height) == 2000


def test_tiff_is_lossless_and_unreadable_so_it_gets_both():
    data = encode(Image.new("RGB", (900, 700), "teal"), "TIFF")
    assert kinds(data) == ["view_png", "view_jpg", "full_jpg"]


def test_webp_is_treated_as_lossy():
    # Lossless-WebP detection is unreliable across Pillow versions, so WebP
    # gets the lossy treatment by design (C3).
    data = encode(Image.new("RGB", (400, 400), "pink"), "WEBP", lossless=True)
    assert kinds(data) == ["view_jpg"]


def test_palette_transparency_survives_into_the_png_variant():
    palette = Image.new("P", (100, 100))
    palette.info["transparency"] = 0
    data = encode(palette, "PNG")
    png = variant(data, "view_png")
    with Image.open(io.BytesIO(png.data)) as out:
        assert out.mode == "RGBA"


def test_transparency_is_flattened_onto_white_for_jpeg():
    rgba = Image.new("RGBA", (100, 100), (255, 0, 0, 0))
    jpg = variant(encode(rgba, "PNG"), "view_jpg")
    with Image.open(io.BytesIO(jpg.data)) as out:
        assert out.mode == "RGB"
        assert out.getpixel((50, 50)) == (255, 255, 255)


def test_cmyk_is_converted_to_rgb():
    data = encode(Image.new("CMYK", (200, 200), (0, 0, 0, 0)), "JPEG")
    with Image.open(io.BytesIO(variant(data, "view_jpg").data)) as out:
        assert out.mode == "RGB"


def test_metadata_is_stripped_from_the_jpeg_variant():
    img = Image.new("RGB", (300, 200), "grey")
    exif = img.getexif()
    exif[271] = "SomeCamera"
    buf = io.BytesIO()
    img.save(buf, "JPEG", exif=exif)
    with Image.open(io.BytesIO(variant(buf.getvalue(), "view_jpg").data)) as out:
        assert dict(out.getexif()) == {}


def test_metadata_is_stripped_from_the_png_variant_too():
    from PIL import PngImagePlugin

    meta = PngImagePlugin.PngInfo()
    meta.add_text("Author", "somebody")
    buf = io.BytesIO()
    Image.new("RGB", (120, 120), "olive").save(buf, "PNG", pnginfo=meta)
    with Image.open(io.BytesIO(variant(buf.getvalue(), "view_png").data)) as out:
        assert "Author" not in out.info


def test_an_oversized_lossless_variant_is_dropped_not_shipped(monkeypatch):
    monkeypatch.setattr(images, "MAX_VARIANT_BYTES", 500)
    noisy = Image.effect_noise((1200, 1200), 120).convert("RGB")
    produced = kinds(encode(noisy, "PNG"))
    assert "view_png" not in produced
    assert "view_jpg" in produced


def test_a_jpeg_variant_is_reencoded_to_fit_the_limit(monkeypatch):
    monkeypatch.setattr(images, "MAX_VARIANT_BYTES", 60_000)
    noisy = Image.effect_noise((1800, 1800), 120).convert("RGB")
    jpg = variant(encode(noisy, "JPEG", quality=100), "view_jpg")
    assert len(jpg.data) <= 60_000


def test_a_shrunken_variant_reports_the_dimensions_it_actually_has(monkeypatch):
    """A Variant whose width/height describe a size its bytes do not have is
    a lie the CLI and the UI would both repeat."""
    monkeypatch.setattr(images, "MAX_VARIANT_BYTES", 20_000)
    noisy = Image.effect_noise((1900, 1900), 120).convert("RGB")
    jpg = variant(encode(noisy, "JPEG", quality=100), "view_jpg")
    with Image.open(io.BytesIO(jpg.data)) as out:
        assert (jpg.width, jpg.height) == out.size


def test_an_oversized_but_supported_original_still_gets_a_full_variant(monkeypatch):
    # Format alone does not make an original usable: the API rejects a
    # supported format that is over 10 MB just as firmly as it rejects HEIC.
    monkeypatch.setattr(images, "MAX_VARIANT_BYTES", 5_000)
    noisy = Image.effect_noise((1500, 1500), 120).convert("RGB")
    assert "full_jpg" in kinds(encode(noisy, "PNG"))


def test_an_original_past_the_api_edge_limit_gets_a_full_variant(monkeypatch):
    monkeypatch.setattr(images, "MAX_API_EDGE", 1000)
    data = encode(Image.new("RGB", (1200, 400), "navy"), "JPEG")
    assert "full_jpg" in kinds(data)
    full = variant(data, "full_jpg")
    assert max(full.width, full.height) <= 1000


def test_directly_usable_needs_format_size_and_dimensions():
    assert images.directly_usable("image/png", 800, 600, 1000) is True
    assert images.directly_usable("image/heic", 800, 600, 1000) is False
    assert images.directly_usable("image/png", 800, 600,
                                  images.MAX_VARIANT_BYTES + 1) is False
    assert images.directly_usable("image/png", 9000, 600, 1000) is False
