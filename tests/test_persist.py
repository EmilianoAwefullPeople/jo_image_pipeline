import json
import sqlite3

import pytest

from jo_pipeline.assets import LoadedAsset
from jo_pipeline.config import REPO_ROOT
from jo_pipeline.group import GroupMember, GroupProposal
from jo_pipeline.manifest import DatasetManifest, ManifestEntry
from jo_pipeline.normalize import MetadataObservation
from jo_pipeline.persist import COMPLETE, PipelineStore


def build_database(tmp_path):
    database_path = tmp_path / "pipeline.sqlite3"
    connection = sqlite3.connect(database_path)
    for script in sorted((REPO_ROOT / "schema").glob("*.sql")):
        connection.executescript(script.read_text())
    connection.commit()
    connection.close()
    return database_path


def build_manifest(paths=("IMG_0001.HEIC",)):
    return DatasetManifest(
        dataset_id="sample_trip",
        dataset_version="1",
        source_root="/datasets/sample_trip",
        created_utc="2026-08-09T00:00:00+00:00",
        entries=[
            ManifestEntry(relative_path=path, media_type="image/heic", size_bytes=100, sha256=f"hash-{path}", modified_utc="2026-08-09T00:00:00+00:00")
            for path in paths
        ],
    )


def build_asset(path="IMG_0001.HEIC"):
    entry = build_manifest((path,)).entries[0]
    observations = [
        MetadataObservation(field="capture_local_time", category="photo", value="2024-07-23T15:10:19", raw_value="2024:07:23 15:10:19", source="exif.DateTimeOriginal", method_version="normalize-1", confidence=1.0),
        MetadataObservation(field="gps_latitude", category="photo", value=None, raw_value=None, source="exif.gps", method_version="normalize-1", unknown_reason="no gps coordinate present"),
    ]
    return LoadedAsset(entry=entry, observations=observations, failure=None)


def build_proposal(paths=("IMG_0001.HEIC",)):
    return GroupProposal(
        label="a moment",
        start_utc="2024-07-23T15:10:19+00:00",
        end_utc="2024-07-23T15:40:19+00:00",
        score=0.82,
        method_version="group-1",
        members=[GroupMember(path, "representative", {"reason": "sharpest member"}) for path in paths],
        evidence={"member_count": len(paths)},
    )


def test_a_missing_database_is_reported_rather_than_created(tmp_path):
    absent = tmp_path / "absent.sqlite3"

    with pytest.raises(FileNotFoundError, match="init_db"):
        PipelineStore(absent)

    assert not absent.exists()


def test_assets_are_stored_and_indexed_by_relative_path(tmp_path):
    database_path = build_database(tmp_path)
    manifest = build_manifest(("IMG_0001.HEIC", "IMG_0002.HEIC"))

    with PipelineStore(database_path) as store:
        store.save_dataset(manifest)
        asset_ids = store.save_assets(manifest)

    assert sorted(asset_ids) == ["IMG_0001.HEIC", "IMG_0002.HEIC"]


def test_storing_the_same_dataset_twice_does_not_duplicate_assets(tmp_path):
    database_path = build_database(tmp_path)
    manifest = build_manifest(("IMG_0001.HEIC",))

    for _ in range(2):
        with PipelineStore(database_path) as store:
            store.save_dataset(manifest)
            store.save_assets(manifest)

    connection = sqlite3.connect(database_path)
    assert connection.execute("select count(*) from media_assets").fetchone()[0] == 1
    connection.close()


def test_a_run_records_its_manifest_and_completion_status(tmp_path):
    database_path = build_database(tmp_path)
    manifest = build_manifest()

    with PipelineStore(database_path) as store:
        store.save_dataset(manifest)
        store.save_assets(manifest)
        run_id = store.start_run(manifest, "manifest-digest", None)
        store.complete_run(run_id, COMPLETE)

    connection = sqlite3.connect(database_path)
    status, digest, version = connection.execute("select status, manifest_sha256, dataset_version from processing_runs where id = ?", (run_id,)).fetchone()
    connection.close()
    assert status == COMPLETE
    assert digest == "manifest-digest"
    assert version == "1"


def test_observations_keep_their_provenance_and_unknown_reason(tmp_path):
    database_path = build_database(tmp_path)
    manifest = build_manifest()

    with PipelineStore(database_path) as store:
        store.save_dataset(manifest)
        asset_ids = store.save_assets(manifest)
        run_id = store.start_run(manifest, "digest", None)
        store.save_observations(run_id, asset_ids, [build_asset()])

    connection = sqlite3.connect(database_path)
    rows = dict(connection.execute("select field, unknown_reason from metadata_observations").fetchall())
    value, source = connection.execute("select value, source from metadata_observations where field = 'capture_local_time'").fetchone()
    connection.close()

    assert rows == {"capture_local_time": None, "gps_latitude": "no gps coordinate present"}
    assert json.loads(value) == "2024-07-23T15:10:19"
    assert source == "exif.DateTimeOriginal"


def test_proposals_store_their_members_and_evidence(tmp_path):
    database_path = build_database(tmp_path)
    manifest = build_manifest()

    with PipelineStore(database_path) as store:
        store.save_dataset(manifest)
        asset_ids = store.save_assets(manifest)
        run_id = store.start_run(manifest, "digest", None)
        store.save_proposals(run_id, manifest.dataset_id, asset_ids, [build_proposal()])

    connection = sqlite3.connect(database_path)
    membership, evidence = connection.execute("select membership, evidence from group_members").fetchone()
    score = connection.execute("select score from group_proposals").fetchone()[0]
    connection.close()

    assert membership == "representative"
    assert json.loads(evidence)["reason"] == "sharpest member"
    assert score == 0.82


def test_a_failure_inside_the_transaction_stores_nothing(tmp_path):
    database_path = build_database(tmp_path)
    manifest = build_manifest()

    with pytest.raises(ValueError):
        with PipelineStore(database_path) as store:
            store.save_dataset(manifest)
            store.save_assets(manifest)
            raise ValueError("stage failed")

    connection = sqlite3.connect(database_path)
    assert connection.execute("select count(*) from media_assets").fetchone()[0] == 0
    connection.close()


def test_members_without_a_stored_asset_are_skipped(tmp_path):
    database_path = build_database(tmp_path)
    manifest = build_manifest()

    with PipelineStore(database_path) as store:
        store.save_dataset(manifest)
        asset_ids = store.save_assets(manifest)
        run_id = store.start_run(manifest, "digest", None)
        store.save_proposals(run_id, manifest.dataset_id, asset_ids, [build_proposal(("IMG_0001.HEIC", "GHOST.HEIC"))])

    connection = sqlite3.connect(database_path)
    assert connection.execute("select count(*) from group_members").fetchone()[0] == 1
    connection.close()
