import hashlib
import json

import pytest

from jo_pipeline.manifest import DatasetManifest, InventoryScanner


def build_dataset(tmp_path, files):
    dataset_root = tmp_path / "datasets"
    source_root = dataset_root / "sample_trip"
    source_root.mkdir(parents=True)
    for name, content in files.items():
        target = source_root / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
    return dataset_root


def test_scan_records_hash_size_and_media_type(tmp_path):
    dataset_root = build_dataset(tmp_path, {"IMG_0001.HEIC": b"heic-bytes"})
    scanner = InventoryScanner(dataset_root, tmp_path / "manifests")

    manifest = scanner.scan("sample_trip", "1")

    assert len(manifest.entries) == 1
    entry = manifest.entries[0]
    assert entry.relative_path == "IMG_0001.HEIC"
    assert entry.media_type == "image/heic"
    assert entry.size_bytes == len(b"heic-bytes")
    assert entry.sha256 == hashlib.sha256(b"heic-bytes").hexdigest()


def test_scan_skips_hidden_files_and_flags_unknown_extensions(tmp_path):
    dataset_root = build_dataset(tmp_path, {
        "IMG_0001.HEIC": b"one",
        ".DS_Store": b"junk",
        "notes.txt": b"known trip facts",
    })
    scanner = InventoryScanner(dataset_root, tmp_path / "manifests")

    manifest = scanner.scan("sample_trip", "1")

    recorded = {entry.relative_path: entry.media_type for entry in manifest.entries}
    assert recorded == {"IMG_0001.HEIC": "image/heic", "notes.txt": "unknown"}


def test_scan_is_ordered_so_manifests_are_reproducible(tmp_path):
    dataset_root = build_dataset(tmp_path, {"IMG_0003.HEIC": b"c", "IMG_0001.HEIC": b"a", "IMG_0002.HEIC": b"b"})
    scanner = InventoryScanner(dataset_root, tmp_path / "manifests")

    manifest = scanner.scan("sample_trip", "1")

    assert [entry.relative_path for entry in manifest.entries] == ["IMG_0001.HEIC", "IMG_0002.HEIC", "IMG_0003.HEIC"]


def test_scan_rejects_a_missing_dataset_folder(tmp_path):
    scanner = InventoryScanner(tmp_path / "datasets", tmp_path / "manifests")

    with pytest.raises(FileNotFoundError):
        scanner.scan("absent_trip", "1")


def test_write_refuses_to_overwrite_an_existing_manifest_version(tmp_path):
    dataset_root = build_dataset(tmp_path, {"IMG_0001.HEIC": b"one"})
    scanner = InventoryScanner(dataset_root, tmp_path / "manifests")
    scanner.write(scanner.scan("sample_trip", "1"))

    with pytest.raises(FileExistsError):
        scanner.write(scanner.scan("sample_trip", "1"))


def test_written_manifest_reloads_to_the_same_entries(tmp_path):
    dataset_root = build_dataset(tmp_path, {"IMG_0001.HEIC": b"one", "IMG_0002.HEIC": b"two"})
    scanner = InventoryScanner(dataset_root, tmp_path / "manifests")
    manifest = scanner.scan("sample_trip", "1")
    target = scanner.write(manifest)

    reloaded = scanner.load("sample_trip", "1")

    assert reloaded == manifest
    assert DatasetManifest.from_dict(json.loads(target.read_text())) == manifest
