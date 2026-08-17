import json
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
from PIL import Image

from jo_web.config import WebConfig
from jo_web.registry import COMPLETE, RunRegistry
from jo_web.service import PipelineService
from jo_web.worker import RunWorker

CAPTURE_BASE = datetime(2026, 5, 4, 9, 30, 0, tzinfo=timezone.utc)


def build_config(tmp_path: Path) -> WebConfig:
    return WebConfig(
        runs_dir=tmp_path / "web_runs",
        max_files=10,
        max_file_bytes=10 * 1024 * 1024,
        max_run_bytes=50 * 1024 * 1024,
        max_pixels=60_000_000,
        run_ttl_minutes=120,
        disk_budget_bytes=1024 * 1024 * 1024,
        llm_concurrency=2,
        openrouter_api_key="test-key",
        openrouter_model="test/model",
        log_level="INFO",
        host="127.0.0.1",
        port=8080,
    )


def evaluation_payload(keep="keep", quality=0.8) -> dict:
    return {
        "caption": "A quiet street in late light",
        "scene": {"scene_type": "cityscape_street", "confidence": 0.8, "evidence": "shopfronts and kerb"},
        "activity": {"description": None, "confidence": 0.2, "evidence": None},
        "landmark": {"name": None, "confidence": 0.1, "evidence": None},
        "visible_text": {"transcription": None, "text_kind": None, "language": None, "confidence": 0.9},
        "screenshot": {"is_screenshot_or_document": False, "travel_relevance": "not_applicable", "document_kind": None, "confidence": 0.95},
        "emotions": [{"label": "calm", "confidence": 0.6}],
        "memory": {"keep_signal": keep, "reason": "Distinctive scene", "confidence": 0.7},
        "representative_quality": {"score": quality, "reasoning": "Clear subject"},
        "journaling_prompt": None,
    }


def build_transport(requests: list, payloads: list | None = None) -> httpx.MockTransport:
    def handler(request):
        requests.append(json.loads(request.content.decode()))
        payload = payloads[len(requests) - 1] if payloads else evaluation_payload()
        body = {
            "id": "gen-abc",
            "choices": [{"message": {"role": "assistant", "content": json.dumps(payload)}}],
            "usage": {"prompt_tokens": 900, "completion_tokens": 400, "cost": 0.01},
        }
        return httpx.Response(200, json=body)

    return httpx.MockTransport(handler)


def write_photo(target: Path, colour: tuple, minutes_offset: int):
    captured = CAPTURE_BASE + timedelta(minutes=minutes_offset)
    exif = Image.Exif()
    exif[0x8769] = {0x9003: captured.strftime("%Y:%m:%d %H:%M:%S"), 0x9011: "+00:00"}
    Image.new("RGB", (160, 120), colour).save(target, format="JPEG", exif=exif)


def seed_run(config: WebConfig, registry: RunRegistry, colours: list) -> str:
    state = registry.create()
    for index, colour in enumerate(colours):
        write_photo(config.upload_dir(state.run_id) / f"IMG_{index:04d}.jpg", colour, index * 3)
    return state.run_id


def test_a_run_produces_grouping_thumbnails_and_one_evaluation_per_image(tmp_path):
    # A complete run must return every stage's output, not just the last one
    config = build_config(tmp_path)
    registry = RunRegistry(config)
    requests = []
    service = PipelineService(config, registry, transport=build_transport(requests))
    run_id = seed_run(config, registry, [(200, 40, 40), (40, 200, 40), (40, 40, 200)])

    service.execute(run_id)

    state = registry.get(run_id)
    assert state.images_analysed == 3
    assert len(state.groups) == 1
    assert state.groups[0].start_utc is not None
    assert [member.membership for member in state.groups[0].members].count("representative") == 1
    assert len(state.thumbnails) == 3
    assert len(state.llm_records) == 3
    assert state.llm_summary.valid == 3
    assert len(requests) == 3
    for key in set(state.thumbnails.values()):
        assert (config.thumbnail_dir(run_id) / f"{key}.jpg").is_file()


def test_byte_identical_uploads_are_evaluated_once_and_still_group_as_duplicates(tmp_path):
    # Duplicate detection is a grouping feature, but paying for the same image twice is not
    config = build_config(tmp_path)
    registry = RunRegistry(config)
    requests = []
    service = PipelineService(config, registry, transport=build_transport(requests))
    state = registry.create()
    payload = None
    for name in ("IMG_0001.jpg", "IMG_0001(1).jpg"):
        target = config.upload_dir(state.run_id) / name
        if payload is None:
            write_photo(target, (10, 120, 200), 0)
            payload = target.read_bytes()
        else:
            target.write_bytes(payload)

    service.execute(state.run_id)

    result = registry.get(state.run_id)
    assert len(requests) == 1
    assert len(result.llm_records) == 1
    memberships = [member.membership for group in result.groups for member in group.members]
    assert "duplicate" in memberships


def test_a_run_with_no_readable_image_fails_the_run_without_killing_the_worker(tmp_path):
    # One broken run must not take the queue down with it
    config = build_config(tmp_path)
    registry = RunRegistry(config)
    service = PipelineService(config, registry, transport=build_transport([]))
    worker = RunWorker(config, registry, service)

    broken = registry.create()
    (config.upload_dir(broken.run_id) / "broken.jpg").write_bytes(b"not an image at all")
    healthy = seed_run(config, registry, [(90, 90, 90), (95, 95, 95)])

    worker._process(broken.run_id)
    worker._process(healthy)

    assert registry.get(broken.run_id).images_analysed == 0
    assert registry.get(healthy).status == COMPLETE
    assert len(registry.get(healthy).groups) == 1


def test_a_leave_out_verdict_reaches_the_api_as_a_dropped_member_with_its_reason(tmp_path):
    # The demo has to show what the model changed, otherwise the evaluation is decorative
    config = replace(build_config(tmp_path), llm_concurrency=1)
    registry = RunRegistry(config)
    requests = []
    payloads = [evaluation_payload(quality=0.95), evaluation_payload(keep="leave_out"), evaluation_payload(quality=0.3)]
    service = PipelineService(config, registry, transport=build_transport(requests, payloads))
    run_id = seed_run(config, registry, [(200, 40, 40), (40, 200, 40), (40, 40, 200)])

    service.execute(run_id)

    state = registry.get(run_id)
    assert len(state.baseline_groups) == 1
    assert len(state.baseline_groups[0].members) == 3
    assert len(state.groups[0].members) == 2
    excluded = state.groups[0].evidence["excluded_by_signal"]
    assert len(excluded) == 1
    assert excluded[0]["reason"] == "Distinctive scene"
    representative = [member for member in state.groups[0].members if member.membership == "representative"]
    assert representative[0].evidence["representative_score"] == 0.95


def test_a_run_without_evaluation_records_keeps_the_deterministic_groups(tmp_path):
    # No key configured must still produce a usable result, not an empty one
    config = replace(build_config(tmp_path), openrouter_api_key="")
    registry = RunRegistry(config)
    service = PipelineService(config, registry, transport=build_transport([]))
    run_id = seed_run(config, registry, [(10, 10, 10), (20, 20, 20)])

    service.execute(run_id)

    state = registry.get(run_id)
    assert state.llm_summary is None
    assert len(state.groups) == 1
    assert state.groups == state.baseline_groups


def test_expired_runs_are_swept_and_recent_runs_are_kept(tmp_path):
    # The demo box has an ephemeral disk that must not fill up
    config = build_config(tmp_path)
    registry = RunRegistry(config)
    stale = registry.create()
    fresh = registry.create()
    stale.created_utc = (datetime.now(timezone.utc) - timedelta(minutes=config.run_ttl_minutes + 5)).isoformat()

    expired = registry.expired()

    assert expired == [stale.run_id]
    registry.delete(stale.run_id)
    assert registry.get(stale.run_id) is None
    assert not config.run_dir(stale.run_id).exists()
    assert registry.get(fresh.run_id) is not None
