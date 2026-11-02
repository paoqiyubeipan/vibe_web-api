import base64

import pytest

from vibe_work_bridge.images import (
    ImageInputError,
    decode_data_url,
    decode_reference_images,
    find_saved_image,
    save_generated_image,
    sniff_mime,
)

PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQ"
    "DwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 32


def png_url(data: bytes = PNG, mime: str = "image/png") -> str:
    return f"data:{mime};base64,{base64.b64encode(data).decode()}"


def test_valid_png_data_url_is_decoded_with_matching_metadata():
    attachment = decode_data_url(png_url(), index=0)
    assert attachment.mime_type == "image/png"
    assert attachment.data == PNG
    assert attachment.name.endswith(".png")
    assert attachment.data_url.startswith("data:image/png;base64,")


def test_remote_url_is_rejected_to_avoid_becoming_a_fetch_proxy():
    with pytest.raises(ImageInputError, match="remote image URLs are not accepted"):
        decode_data_url("https://example.com/picture.png")


def test_declared_type_must_match_the_actual_bytes():
    with pytest.raises(ImageInputError, match="declares"):
        decode_data_url(png_url(JPEG, mime="image/png"))


def test_non_image_payload_is_rejected():
    with pytest.raises(ImageInputError, match="does not look like"):
        decode_data_url(f"data:image/png;base64,{base64.b64encode(b'<html></html>').decode()}")


def test_oversized_image_is_rejected_before_upload():
    with pytest.raises(ImageInputError, match="limit"):
        decode_data_url(png_url(), max_bytes=8)


def test_unsupported_type_and_bad_base64_are_rejected():
    with pytest.raises(ImageInputError, match="unsupported image type"):
        decode_data_url(f"data:image/tiff;base64,{base64.b64encode(PNG).decode()}")
    with pytest.raises(ImageInputError, match="not valid base64"):
        decode_data_url("data:image/png;base64,not*base64")


def test_reference_batch_enforces_count_and_total_size():
    with pytest.raises(ImageInputError, match="at most 4"):
        decode_reference_images([png_url() for _ in range(5)])
    with pytest.raises(ImageInputError, match="total"):
        decode_reference_images([png_url(), png_url()], max_total_bytes=10)
    assert len(decode_reference_images([png_url(), png_url()])) == 2


def test_sniff_mime_recognises_common_formats():
    assert sniff_mime(PNG) == "image/png"
    assert sniff_mime(JPEG) == "image/jpeg"
    assert sniff_mime(b"GIF89a....") == "image/gif"
    assert sniff_mime(b"not an image at all") is None


def test_saved_image_can_be_looked_up_and_traversal_is_refused(tmp_path, monkeypatch):
    monkeypatch.setenv("VIBE_WORK_OUTPUT_DIR", str(tmp_path))
    image = save_generated_image(PNG, "image/png")
    path = find_saved_image(image.image_id)
    assert path is not None and path.read_bytes() == PNG
    assert find_saved_image("../../etc/passwd") is None
    assert find_saved_image("deadbeef") is None
