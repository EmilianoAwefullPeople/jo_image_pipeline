import json
import sqlite3

import pytest

from jo_pipeline.assets import LoadedAsset
from jo_pipeline.config import REPO_ROOT
from jo_pipeline.group import GroupMember, GroupProposal
from jo_pipeline.manifest import DatasetManifest, ManifestEntry
from jo_pipeline.normalize import MetadataObservation
from jo_pipeline.persist import COMPLETE, OPENROUTER_PROVIDER, ModelCall, PipelineStore
from jo_pipeline.refine import build_llm_observations
from tests.test_refine import evaluation


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


def test_llm_observations_store_under_the_fetched_category_the_schema_reserves(tmp_path):
    # The category CHECK constraint must accept externally obtained metadata without a schema change
    database_path = build_database(tmp_path)
    manifest = build_manifest()
    observations = build_llm_observations(evaluation(keep="leave_out", quality=0.91), "openai/gpt-5.6-sol", "p1/llm-eval-1")

    with PipelineStore(database_path) as store:
        store.save_dataset(manifest)
        asset_ids = store.save_assets(manifest)
        run_id = store.start_run(manifest, "digest", "openai/gpt-5.6-sol")
        stored = store.save_path_observations(run_id, asset_ids, {"IMG_0001.HEIC": observations})

    connection = sqlite3.connect(database_path)
    categories = {row[0] for row in connection.execute("select distinct category from metadata_observations").fetchall()}
    keep, confidence, evidence = connection.execute(
        "select value, confidence, evidence from metadata_observations where field = 'memory_keep_signal'"
    ).fetchone()
    source = connection.execute("select distinct source from metadata_observations").fetchone()[0]
    landmark_reason = connection.execute("select unknown_reason from metadata_observations where field = 'landmark_candidate'").fetchone()[0]
    model_id = connection.execute("select model_id from processing_runs where id = ?", (run_id,)).fetchone()[0]
    connection.close()

    assert stored == len(observations)
    assert categories == {"fetched"}
    assert json.loads(keep) == "leave_out"
    assert confidence == 0.77
    assert json.loads(evidence)["reason"] == "a reason"
    assert source == "llm.openai/gpt-5.6-sol"
    assert landmark_reason == "model returned null"
    assert model_id == "openai/gpt-5.6-sol"


def test_model_calls_store_their_cost_accounting(tmp_path):
    # model_calls has existed unwritten since Week 2; this is the contract it was designed for
    database_path = build_database(tmp_path)
    manifest = build_manifest()
    call = ModelCall(
        relative_path="IMG_0001.HEIC",
        provider=OPENROUTER_PROVIDER,
        model_id="openai/gpt-5.6-sol",
        prompt_version="1",
        schema_version="llm-eval-1",
        attempt=2,
        validation_status="valid",
        latency_ms=9970,
        prompt_tokens=2373,
        completion_tokens=457,
        cost_usd=0.0285,
        parsed_output={"caption": "a caption"},
        failure_detail=None,
        called_utc="2026-08-17T06:00:00+00:00",
    )

    with PipelineStore(database_path) as store:
        store.save_dataset(manifest)
        asset_ids = store.save_assets(manifest)
        run_id = store.start_run(manifest, "digest", "openai/gpt-5.6-sol")
        stored = store.save_model_calls(run_id, asset_ids, [call])

    connection = sqlite3.connect(database_path)
    provider, status, attempt, cost, tokens, parsed = connection.execute(
        "select provider, validation_status, attempt, cost_usd, prompt_tokens, parsed_output from model_calls"
    ).fetchone()
    connection.close()

    assert stored == 1
    assert provider == "openrouter"
    assert status == "valid"
    assert attempt == 2
    assert cost == 0.0285
    assert tokens == 2373
    assert json.loads(parsed)["caption"] == "a caption"


def test_a_model_call_for_an_unstored_asset_is_skipped_rather_than_failing(tmp_path):
    database_path = build_database(tmp_path)
    manifest = build_manifest()

    with PipelineStore(database_path) as store:
        store.save_dataset(manifest)
        asset_ids = store.save_assets(manifest)
        run_id = store.start_run(manifest, "digest", None)
        stored = store.save_path_observations(run_id, asset_ids, {"GHOST.HEIC": build_llm_observations(evaluation(), "m", "v")})

    assert stored == 0
    connection = sqlite3.connect(database_path)
    assert connection.execute("select count(*) from metadata_observations").fetchone()[0] == 0
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
