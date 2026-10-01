import io

from PIL import Image

from app import images
from tests.conftest import encode


def test_detects_jpeg(jpeg_bytes):
    assert images.detect_media_type(jpeg_bytes) == "image/jpeg"


def test_detects_png(png_bytes):
    assert images.detect_media_type(png_bytes) == "image/png"


def test_detects_heic(heic):
    assert images.detect_media_type(heic) == "image/heic"


def test_detection_ignores_any_filename_because_it_never_sees_one():
    # The signature is the only input. iOS uploads HEIC from Files and JPEG
    # from the camera roll under indistinguishable names.
    assert images.detect_media_type.__code__.co_argcount == 1


def test_non_image_is_raw():
    result = images.normalise(b"plain text, not an image")
    assert result.kind == "raw"
    assert result.variants == []
    assert result.media_type == "application/octet-stream"


def test_jpeg_produces_only_a_jpeg_view(jpeg_bytes):
    result = images.normalise(jpeg_bytes)
    assert result.kind == "image"
    assert [v.kind for v in result.variants] == ["view_jpg"]


def test_long_edge_is_capped_at_2000(jpeg_bytes):
    view = images.normalise(jpeg_bytes).variants[0]
    assert (view.width, view.height) == (2000, 1333)


def test_portrait_is_capped_on_its_own_long_edge():
    data = encode(Image.new("RGB", (1500, 4000), "green"), "JPEG")
    view = images.normalise(data).variants[0]
    assert (view.width, view.height) == (750, 2000)


def test_small_image_is_not_upscaled(png_bytes):
    view = images.normalise(png_bytes).variants[0]
    assert (view.width, view.height) == (800, 600)


def test_original_dimensions_are_reported_not_the_view(jpeg_bytes):
    result = images.normalise(jpeg_bytes)
    assert (result.width, result.height) == (3000, 2000)


def test_exif_orientation_is_applied_to_the_pixels():
    # Orientation 6 is "rotate 90 degrees clockwise on display": 200x100 in,
    # 100x200 out.
    img = Image.new("RGB", (200, 100), "purple")
    exif = img.getexif()
    exif[274] = 6
    buf = io.BytesIO()
    img.save(buf, "JPEG", exif=exif)
    view = images.normalise(buf.getvalue()).variants[0]
    assert (view.width, view.height) == (100, 200)


def test_corrupt_image_fails_explicitly(png_bytes):
    broken = png_bytes[:40]
    result = images.normalise(broken)
    assert result.kind == "failed"
    assert result.error
    assert result.variants == []


def test_source_over_the_pixel_limit_is_refused(monkeypatch):
    # Between 1x and 2x the limit, so Pillow's own bomb check (which trips
    # at 2x) cannot be what rejects it.
    monkeypatch.setattr(images, "MAX_SOURCE_PIXELS", 200_000)
    data = encode(Image.new("RGB", (500, 500), "white"), "PNG")  # 250_000 px
    result = images.normalise(data)
    assert result.kind == "failed"
    assert "pixel" in result.error.lower()


def test_animated_gif_uses_the_first_frame():
    red = Image.new("RGB", (60, 60), (255, 0, 0))
    blue = Image.new("RGB", (60, 60), (0, 0, 255))
    buf = io.BytesIO()
    red.save(buf, "GIF", save_all=True, append_images=[blue])
    view = images.normalise(buf.getvalue()).variants[0]
    with Image.open(io.BytesIO(view.data)) as out:
        r, g, b = out.convert("RGB").getpixel((30, 30))
        assert r > 200 and b < 60      # the red frame, not the blue one
