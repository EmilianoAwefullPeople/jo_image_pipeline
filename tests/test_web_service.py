import json
import re
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
from fastapi.encoders import jsonable_encoder
from PIL import Image, ImageDraw

from jo_web.config import WebConfig
from jo_web.registry import COMPLETE, RunRegistry
from jo_web.serialize import export_payload
from jo_web.service import PipelineService
from jo_web.worker import RunWorker
from llm_pipeline.prompts import PROMPT_VERSION, custom_prompts, default_prompts

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
        "general_description": "A quiet street in late light with shopfronts running down one side of the kerb",
        "scene_setting": {"types": ["street"], "other_detail": None},
        "landmark": {"name": None, "confidence_tier": None, "evidence": None},
        "notable_subjects": [],
        "focal_points": ["no_clear_subject"],
        "activity": {"types": [], "other_detail": None},
        "environment": {"types": ["city"], "other_detail": None, "specific_style": None},
        "composition": ["wide_landscape"],
        "weather": ["overcast"],
        "keyword_tags": ["street", "shopfronts", "late light"],
        "photographic_style": {"types": ["muted_desaturated"], "other_detail": None},
        "why_tags": [],
        "screenshot": {"is_screenshot_or_document": False, "travel_relevance": "not_applicable", "document_kind": None, "confidence": 0.95},
        "memory": {"keep_signal": keep, "reason": "Distinctive scene", "confidence": 0.7},
        "representative_quality": {"score": quality, "reasoning": "Clear subject"},
    }


def review_payload(frame_count: int, moments: list | None = None) -> dict:
    ranges = moments or [(0, frame_count - 1)]
    return {
        "moments": [{"first_frame": first, "last_frame": last, "title": f"Moment from {first}", "reason": "The descriptions read as one place."} for first, last in ranges],
        "leave_out": [],
    }


def topic_payload(groups: list | None) -> dict:
    return {"groups": [{"frames": frames, "title": title, "about": "Food on the street.", "why": why} for frames, title, why in (groups or [])]}


def build_transport(requests: list, payloads: list | None = None, review_moments: list | None = None, topic_groups: list | None = None) -> httpx.MockTransport:
    evaluations = []

    def handler(request):
        body = json.loads(request.content.decode())
        requests.append(body)
        schema_name = body["response_format"]["json_schema"]["name"]
        if schema_name == "moment_review":
            frame_count = int(re.search(r"Session of (\d+) photos", body["messages"][1]["content"]).group(1))
            payload = review_payload(frame_count, review_moments)
        elif schema_name == "topic_review":
            payload = topic_payload(topic_groups)
        else:
            evaluations.append(body)
            payload = payloads[len(evaluations) - 1] if payloads else evaluation_payload()
        response = {
            "id": "gen-abc",
            "choices": [{"message": {"role": "assistant", "content": json.dumps(payload)}}],
            "usage": {"prompt_tokens": 900, "completion_tokens": 400, "cost": 0.01},
        }
        return httpx.Response(200, json=response)

    return httpx.MockTransport(handler)


def review_requests(requests: list) -> list:
    return [body for body in requests if body["response_format"]["json_schema"]["name"] == "moment_review"]


def topic_requests(requests: list) -> list:
    return [body for body in requests if body["response_format"]["json_schema"]["name"] == "topic_review"]


def write_photo(target: Path, colour: tuple, minutes_offset: int, pattern: int = 0):
    # A vertical bar at a per-photo column keeps the difference hashes apart, so the photos group as distinct frames rather than duplicates
    captured = CAPTURE_BASE + timedelta(minutes=minutes_offset)
    exif = Image.Exif()
    exif[0x8769] = {0x9003: captured.strftime("%Y:%m:%d %H:%M:%S"), 0x9011: "+00:00"}
    image = Image.new("RGB", (160, 120), colour)
    left = 10 + (pattern * 45) % 130
    ImageDraw.Draw(image).rectangle([left, 0, left + 15, 120], fill=(255, 255, 255))
    image.save(target, format="JPEG", exif=exif)


def seed_run(config: WebConfig, registry: RunRegistry, colours: list) -> str:
    state = registry.create()
    for index, colour in enumerate(colours):
        write_photo(config.upload_dir(state.run_id) / f"IMG_{index:04d}.jpg", colour, index * 3, pattern=index)
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
    assert len(requests) == 4
    assert len(review_requests(requests)) == 1
    assert state.review_summary.valid == 1
    assert state.groups[0].evidence["review"]["title"] == "Moment from 0"
    assert state.groups[0].method_version == "regroup-1"
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
    assert result.review_summary is None
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

    worker._process(broken.run_id, service.execute)
    worker._process(healthy, service.execute)

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


def test_a_review_that_splits_an_outing_reaches_the_page_with_its_titles_and_the_new_boundary(tmp_path):
    # The review is only worth showing if the viewer can see what it changed and why
    config = build_config(tmp_path)
    registry = RunRegistry(config)
    requests = []
    service = PipelineService(config, registry, transport=build_transport(requests, review_moments=[(0, 0), (1, 2)]))
    run_id = seed_run(config, registry, [(200, 40, 40), (40, 200, 40), (40, 40, 200)])

    service.execute(run_id)

    state = registry.get(run_id)
    assert len(state.baseline_groups) == 1
    assert [len(group.members) for group in state.groups] == [1, 2]
    assert [group.evidence["review"]["title"] for group in state.groups] == ["Moment from 0", "Moment from 1"]
    assert [group.evidence["review"]["change"] for group in state.groups] == ["split", "split"]
    assert state.groups[0].evidence["closed_by"]["kind"] == "review_split"
    assert state.groups[1].evidence["opened_by"] == state.groups[0].evidence["closed_by"]
    assert len(state.review_records) == 1
    assert "IMG_" not in review_requests(requests)[0]["messages"][1]["content"]


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


def test_a_prompt_attached_to_a_run_is_the_one_sent_and_recorded(tmp_path):
    # The editor is only useful if the edited text reaches the request and the record says so
    config = build_config(tmp_path)
    registry = RunRegistry(config)
    requests = []
    service = PipelineService(config, registry, transport=build_transport(requests))
    run_id = seed_run(config, registry, [(200, 40, 40)])
    registry.set_prompts(run_id, custom_prompts("Answer only in Latin.", "Taken at {capture_local_time}."))

    service.execute(run_id)

    state = registry.get(run_id)
    assert requests[0]["messages"][0]["content"] == "Answer only in Latin."
    assert requests[0]["messages"][1]["content"][1]["text"] == "Taken at 2026:05:04 09:30:00."
    assert state.llm_summary.prompt_version == "custom"
    assert state.llm_records[0].prompt_version == "custom"


def test_a_run_with_no_attached_prompt_uses_the_stored_default(tmp_path):
    config = build_config(tmp_path)
    registry = RunRegistry(config)
    requests = []
    service = PipelineService(config, registry, transport=build_transport(requests))
    run_id = seed_run(config, registry, [(40, 40, 200)])

    service.execute(run_id)

    assert requests[0]["messages"][0]["content"] == default_prompts().system
    assert registry.get(run_id).llm_summary.prompt_version == PROMPT_VERSION


def test_the_export_carries_aggregates_every_image_and_every_applied_style(tmp_path):
    # The download must stand alone: coverage counts, everything about each image, and every style applied to the run
    config = build_config(tmp_path)
    registry = RunRegistry(config)
    requests = []
    service = PipelineService(config, registry, transport=build_transport(requests, topic_groups=[([0, 2], "Noodles and tea", ["fun"])]))
    run_id = seed_run(config, registry, [(200, 40, 40), (40, 200, 40), (40, 40, 200)])
    service.execute(run_id)
    registry.set_style(run_id, "foodie_tour")
    service.group_by_style(run_id)

    payload = export_payload(registry.get(run_id), 0)

    coverage = payload["aggregate"]["coverage"]
    assert payload["aggregate"]["images_analysed"] == 3
    assert payload["aggregate"]["evaluations"] == 3
    # Extraction fields count over analysed images, with the two gps rows folded into one labelled entry
    assert coverage["Capture Time (local)"] == "3/3"
    assert coverage["GPS Location"] == "0/3"
    assert "gps_latitude" not in coverage
    # LLM fields count over valid evaluations; the mock evaluation has no landmark and no activity
    assert coverage["Description"] == "3/3"
    assert coverage["Landmark"] == "0/3"
    assert coverage["Activity"] == "0/3"

    image = payload["images"]["IMG_0000.jpg"]
    assert image["sha256"]
    assert image["size_bytes"] > 0
    assert image["observations"]
    assert image["evaluation"]["general_description"] == evaluation_payload()["general_description"]

    # Both applied styles are present with their groups; the top-level groups still show only the latest style
    assert set(payload["styles"]) == {"moments", "foodie_tour"}
    assert payload["styles"]["moments"]["groups"][0]["method_version"] == "regroup-1"
    assert payload["styles"]["foodie_tour"]["groups"][0]["method_version"] == "topic-foodie_tour-1"
    assert payload["styles"]["foodie_tour"]["groups"][0]["evidence"]["review"]["title"] == "Noodles and tea"
    assert payload["styles"]["foodie_tour"]["review"]["unassigned"] == ["IMG_0001.jpg"]
    assert payload["groups"][0]["method_version"] == "topic-foodie_tour-1"

    # The whole export, real EXIF observations included, must survive JSON encoding
    json.dumps(jsonable_encoder(payload))


def test_reapplying_a_style_overwrites_only_that_styles_result(tmp_path):
    # A rerun of one style must not multiply entries or disturb the other styles' saved results
    config = build_config(tmp_path)
    registry = RunRegistry(config)
    service = PipelineService(config, registry, transport=build_transport([], topic_groups=[([0, 1], "Noodles and tea", ["fun"])]))
    run_id = seed_run(config, registry, [(200, 40, 40), (40, 200, 40), (40, 40, 200)])
    service.execute(run_id)
    registry.set_style(run_id, "foodie_tour")
    service.group_by_style(run_id)

    registry.set_style(run_id, "moments")
    service.group_by_style(run_id)

    state = registry.get(run_id)
    assert set(state.style_results) == {"moments", "foodie_tour"}
    assert state.style_results["moments"].groups[0].method_version == "regroup-1"
    assert state.style_results["foodie_tour"].groups[0].method_version == "topic-foodie_tour-1"


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


def test_a_completed_run_can_be_regrouped_by_a_style_from_its_cached_text_without_re_evaluating(tmp_path):
    # The drop-down asks a different question of the same descriptions; the photos are never sent again
    config = build_config(tmp_path)
    registry = RunRegistry(config)
    requests = []
    service = PipelineService(config, registry, transport=build_transport(requests, topic_groups=[([0, 2], "Noodles and tea", ["fun"])]))
    run_id = seed_run(config, registry, [(200, 40, 40), (40, 200, 40), (40, 40, 200)])
    service.execute(run_id)
    evaluations_before = len(requests) - len(review_requests(requests))

    registry.set_style(run_id, "foodie_tour")
    service.group_by_style(run_id)

    state = registry.get(run_id)
    assert len(topic_requests(requests)) == 1
    assert len(requests) - len(review_requests(requests)) - len(topic_requests(requests)) == evaluations_before
    assert "meals and food or drink stops" in topic_requests(requests)[0]["messages"][0]["content"]
    assert [member.relative_path for member in state.groups[0].members] == ["IMG_0000.jpg", "IMG_0002.jpg"]
    assert state.groups[0].evidence["review"]["title"] == "Noodles and tea"
    assert state.groups[0].evidence["review"]["why"] == ["fun"]
    assert state.groups[0].method_version == "topic-foodie_tour-1"
    assert state.review_summary.style == "foodie_tour"
    assert state.review_unassigned == ["IMG_0001.jpg"]
    assert len(state.baseline_groups) == 1

    service.group_by_style(run_id)
    assert len(topic_requests(requests)) == 1

    registry.set_style(run_id, "moments")
    service.group_by_style(run_id)
    assert len(review_requests(requests)) == 1
    assert registry.get(run_id).groups[0].method_version == "regroup-1"
    assert registry.get(run_id).review_unassigned == []
