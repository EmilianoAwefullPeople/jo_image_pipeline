from PIL import Image
from PIL.TiffImagePlugin import IFDRational

from jo_pipeline.extract import PhotoExtractor, json_safe
from jo_pipeline.manifest import ManifestEntry


def build_entry(relative_path, media_type="image/jpeg"):
    return ManifestEntry(
        relative_path=relative_path,
        media_type=media_type,
        size_bytes=1,
        sha256="0" * 64,
        modified_utc="2026-08-09T00:00:00+00:00",
    )


def write_image(root, name, colour):
    root.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (64, 48), colour).save(root / name)


def test_json_safe_converts_exif_value_types():
    assert json_safe(b"\x00\x01") == "0001"
    assert json_safe(IFDRational(1, 2)) == 0.5
    assert json_safe((IFDRational(37, 1), IFDRational(38, 1))) == [37.0, 38.0]
    assert json_safe(None) is None


def test_json_safe_keeps_an_undefined_rational_as_text():
    assert json_safe(IFDRational(1, 0)) == str(IFDRational(1, 0))


def test_extract_records_visual_signals_and_dimensions(tmp_path):
    write_image(tmp_path, "IMG_0001.PNG", (10, 120, 200))
    extraction = PhotoExtractor(tmp_path).extract(build_entry("IMG_0001.PNG", "image/png"))

    assert extraction.failures == []
    assert extraction.image_properties["width"] == 64
    assert extraction.image_properties["height"] == 48
    assert len(extraction.visual_signals["difference_hash"]) == 16
    assert extraction.visual_signals["palette"][0]["hex"] == "#0a78c8"


def test_extract_skips_media_that_is_not_an_image(tmp_path):
    extraction = PhotoExtractor(tmp_path).extract(build_entry("CLIP.MP4", "video/mp4"))

    assert extraction.failures == ["media type video/mp4 is not an image"]
    assert extraction.exif_tags == {}


def test_extract_records_a_failure_for_an_unreadable_file(tmp_path):
    (tmp_path / "BROKEN.JPG").write_bytes(b"not an image")
    extraction = PhotoExtractor(tmp_path).extract(build_entry("BROKEN.JPG"))

    assert len(extraction.failures) == 1
    assert extraction.visual_signals == {}


def test_palette_is_repeatable_across_runs(tmp_path):
    write_image(tmp_path, "IMG_0001.PNG", (10, 120, 200))
    extractor = PhotoExtractor(tmp_path)

    first = extractor.extract(build_entry("IMG_0001.PNG", "image/png"))
    second = extractor.extract(build_entry("IMG_0001.PNG", "image/png"))

    assert first.visual_signals["palette"] == second.visual_signals["palette"]
