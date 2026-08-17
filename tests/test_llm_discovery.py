import hashlib

import pytest

from llm_pipeline.discovery import discover_images


def write_file(root, name, payload=b"media-bytes"):
    path = root / name
    path.write_bytes(payload)
    return path


def test_images_are_discovered_sorted_with_their_content_hash(tmp_path):
    write_file(tmp_path, "IMG_0002.HEIC", b"heic-bytes")
    write_file(tmp_path, "IMG_0001.jpg", b"jpeg-bytes")
    write_file(tmp_path, "IMG_0003.png", b"png-bytes")

    images = discover_images(tmp_path)

    assert [image.relative_path for image in images] == ["IMG_0001.jpg", "IMG_0002.HEIC", "IMG_0003.png"]
    assert images[0].sha256 == hashlib.sha256(b"jpeg-bytes").hexdigest()
    assert images[0].size_bytes == len(b"jpeg-bytes")


def test_videos_documents_archives_and_dotfiles_are_skipped(tmp_path):
    write_file(tmp_path, "IMG_0001.jpg")
    write_file(tmp_path, "clip.MP4")
    write_file(tmp_path, "clip.mov")
    write_file(tmp_path, "notes.docx")
    write_file(tmp_path, "archive.zip")
    write_file(tmp_path, ".hidden.jpg")

    images = discover_images(tmp_path)

    assert [image.relative_path for image in images] == ["IMG_0001.jpg"]


def test_a_missing_dataset_folder_is_reported_rather_than_created(tmp_path):
    with pytest.raises(FileNotFoundError):
        discover_images(tmp_path / "missing")
