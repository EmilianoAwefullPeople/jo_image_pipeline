import json

import pytest

from llm_pipeline.store import ImageEvaluationRecord, RunStore, RunSummary


def build_record(sha256="a" * 64) -> ImageEvaluationRecord:
    return ImageEvaluationRecord(
        dataset_id="Trip",
        relative_path="IMG_0001.HEIC",
        sha256=sha256,
        model="test/model",
        prompt_version="1",
        schema_version="llm-eval-1",
        derivative_version="derivative-1",
        evaluated_utc="2026-08-16T10:00:00+00:00",
        attempts=1,
        latency_ms=1200,
        prompt_tokens=900,
        completion_tokens=400,
        cost_usd=0.0165,
        generation_id="gen-abc",
        validation_status="valid",
        evaluation={"caption": "a test"},
        failure_detail=None,
    )


def build_summary() -> RunSummary:
    return RunSummary(
        dataset_id="Trip",
        model="test/model",
        prompt_version="1",
        schema_version="llm-eval-1",
        started_utc="2026-08-16T10:00:00+00:00",
        finished_utc="2026-08-16T10:05:06+00:00",
        images_discovered=10,
        images_skipped_existing=4,
        images_evaluated=6,
        valid=5,
        invalid=1,
        request_failed=0,
        total_prompt_tokens=5400,
        total_completion_tokens=2400,
        total_cost_usd=0.099,
    )


def build_store(tmp_path) -> RunStore:
    return RunStore.for_versions(tmp_path / "llm_runs", "Trip", "1", "llm-eval-1")


def test_a_record_is_written_once_and_a_second_write_is_refused(tmp_path):
    # Records are immutable so a paid evaluation can never be silently overwritten
    store = build_store(tmp_path)

    target = store.write_record(build_record())

    assert json.loads(target.read_text())["sha256"] == "a" * 64
    with pytest.raises(FileExistsError):
        store.write_record(build_record())


def test_exists_reports_written_records_so_reruns_skip_paid_work(tmp_path):
    store = build_store(tmp_path)

    assert store.exists("a" * 64) is False
    store.write_record(build_record())
    assert store.exists("a" * 64) is True


def test_the_artifact_directory_embeds_prompt_and_schema_versions(tmp_path):
    store = build_store(tmp_path)

    target = store.write_record(build_record())

    assert target.parent.name == "p1-llm-eval-1"
    assert target.name == f"{'a' * 16}.json"


def test_a_store_opened_on_an_existing_run_directory_reads_its_records(tmp_path):
    # jo_pipeline must be able to read whichever prompt/schema run exists on disk, not only the current one
    build_store(tmp_path).write_record(build_record())

    records = RunStore(tmp_path / "llm_runs" / "Trip" / "p1-llm-eval-1").records()

    assert len(records) == 1
    assert records[0]["schema_version"] == "llm-eval-1"


def test_run_summaries_are_timestamped_files_in_the_run_directory(tmp_path):
    store = build_store(tmp_path)

    target = store.write_summary(build_summary())

    assert target.name == "run-20260816T100506Z.json"
    assert json.loads(target.read_text())["total_cost_usd"] == 0.099
