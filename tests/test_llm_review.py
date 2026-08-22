import json

import httpx
import pytest
from pydantic import ValidationError

from llm_pipeline.client import OpenRouterClient
from llm_pipeline.evaluate import INVALID, VALID
from llm_pipeline.prompts import REVIEW_PROMPT_VERSION
from llm_pipeline.review import ReviewFrame, ReviewStore, build_session, load_reviews, render_session, reviewer_for
from llm_pipeline.schema import REVIEW_SCHEMA_NAME, REVIEW_SCHEMA_VERSION, TOPIC_SCHEMA_NAME, MomentReview, TopicReview
from llm_pipeline.styles import FOODIE_TOUR, MOMENTS, style_for

MOMENTS_STYLE = style_for(MOMENTS)


def frame(index, minute, summary="a street scene | keep: keep", gap=None, distance=None, boundary=None, sha=None) -> ReviewFrame:
    return ReviewFrame(
        index=index,
        relative_path=f"IMG_{index:04d}.jpg",
        sha256=sha or f"{index:064x}",
        captured_utc=f"2026-05-08T09:{minute:02d}:00+00:00",
        gap_seconds=gap,
        distance_metres=distance,
        boundary_before=boundary,
        summary=summary,
    )


def session_of(frames, source="p1-llm-eval-1", review_version=REVIEW_PROMPT_VERSION, schema_version=REVIEW_SCHEMA_VERSION):
    return build_session("Trip", source, frames, review_version, schema_version)


def three_frames():
    boundary = {"kind": "place_change", "after": "IMG_0001.jpg", "distance_metres": 180.0, "threshold_metres": 150.0}
    return [
        frame(0, 3),
        frame(1, 3, gap=7.0, distance=1.5),
        frame(2, 16, gap=780.0, distance=180.0, boundary=boundary, summary="street signs on the same road | landmark: The Bund | keep: keep"),
    ]


def three_frame_session():
    return session_of(three_frames())


def moment_store(tmp_path) -> ReviewStore:
    return ReviewStore(tmp_path / "llm_runs", "Trip", REVIEW_PROMPT_VERSION, REVIEW_SCHEMA_VERSION)


def moment_reviewer(client, store):
    return reviewer_for(client, store, "Trip", MOMENTS_STYLE)


def review_payload(moments=((0, 2),), leave_out=()) -> dict:
    return {
        "moments": [{"first_frame": first, "last_frame": last, "title": "Morning on the Bund", "reason": "Same road and activity across a short gap."} for first, last in moments],
        "leave_out": [{"frame": index, "reason": "a practical screenshot", "confidence": 0.8} for index in leave_out],
    }


def completion_body(content, cost=0.01) -> dict:
    return {
        "id": "gen-review",
        "choices": [{"message": {"role": "assistant", "content": content}}],
        "usage": {"prompt_tokens": 1200, "completion_tokens": 150, "cost": cost},
    }


def build_client(bodies, requests) -> OpenRouterClient:
    def handler(request):
        requests.append(json.loads(request.content.decode()))
        return httpx.Response(200, json=bodies[len(requests) - 1])

    return OpenRouterClient("test-key", "test/model", transport=httpx.MockTransport(handler))


def test_a_review_must_cover_every_frame_exactly_once_in_order():
    # The applier maps frame ranges straight onto members, so a gap or overlap would lose or duplicate photos
    MomentReview.model_validate(review_payload(moments=((0, 1), (2, 2))))

    with pytest.raises(ValidationError, match="contiguous"):
        MomentReview.model_validate(review_payload(moments=((0, 1), (3, 4))))
    with pytest.raises(ValidationError, match="contiguous"):
        MomentReview.model_validate(review_payload(moments=((0, 2), (2, 3))))
    with pytest.raises(ValidationError, match="contiguous"):
        MomentReview.model_validate(review_payload(moments=((1, 2),)))
    with pytest.raises(ValidationError, match="before first_frame"):
        MomentReview.model_validate(review_payload(moments=((2, 0),)))


def test_leave_out_frames_must_be_unique_and_inside_the_session():
    with pytest.raises(ValidationError, match="more than once"):
        MomentReview.model_validate(review_payload(leave_out=(1, 1)))
    with pytest.raises(ValidationError, match="outside"):
        MomentReview.model_validate(review_payload(leave_out=(7,)))


def test_a_review_that_stops_short_of_the_session_is_rejected_by_covers():
    review = MomentReview.model_validate(review_payload(moments=((0, 1),)))

    with pytest.raises(ValueError, match="session has 3"):
        review.covers(3)


def test_the_rendered_session_carries_boundaries_times_and_gaps_but_never_paths_or_coordinates():
    # What leaves the machine is text: descriptions, UTC clock times, gaps and metre distances, nothing else
    text = render_session(three_frame_session())

    assert text.startswith("Session of 3 photos in capture order, proposed as 2 moments. Times are UTC.")
    assert "Day 2026-05-08" in text
    assert "[0] 09:03 a street scene" in text
    assert "[1] 09:03 (+7 s, 2 m) a street scene" in text
    assert "--- proposed boundary: moved 180 m, threshold 150 m ---" in text
    assert "[2] 09:16 (+13 min, 180 m) street signs" in text
    assert "IMG_" not in text
    assert "31." not in text and "121." not in text


def test_the_session_id_changes_with_the_frame_text_the_boundaries_and_the_style_but_not_the_paths():
    base = three_frame_session()
    reworded = session_of([frame(0, 3, summary="different words"), base.frames[1], base.frames[2]])
    unbounded = session_of([base.frames[0], base.frames[1], ReviewFrame(**dict(vars(base.frames[2]), boundary_before=None))])
    renamed = session_of([ReviewFrame(**dict(vars(base.frames[0]), relative_path="other.jpg")), base.frames[1], base.frames[2]])
    other_source = session_of(base.frames, source="p2-llm-eval-2")
    other_style = session_of(base.frames, review_version="topic-foodie_tour-1", schema_version="topic-review-1")

    assert reworded.session_id != base.session_id
    assert unbounded.session_id != base.session_id
    assert other_source.session_id != base.session_id
    assert other_style.session_id != base.session_id
    assert renamed.session_id == base.session_id


def test_a_valid_review_is_stored_with_its_cost_and_the_request_demands_the_review_schema(tmp_path):
    requests = []
    client = build_client([completion_body(json.dumps(review_payload()))], requests)
    store = moment_store(tmp_path)
    session = three_frame_session()

    run = moment_reviewer(client, store).review([session])

    assert run.summary.valid == 1
    assert run.summary.style == MOMENTS
    assert run.summary.total_cost_usd == 0.01
    assert run.records[0].review["moments"][0]["title"] == "Morning on the Bund"
    assert run.records[0].source_version == "p1-llm-eval-1"
    assert run.records[0].review_prompt_version == REVIEW_PROMPT_VERSION
    assert run.records[0].style == MOMENTS
    assert store.run_dir.name == f"review-r{REVIEW_PROMPT_VERSION}-{REVIEW_SCHEMA_VERSION}"
    assert store.exists(session.session_id)
    body = requests[0]
    assert body["response_format"]["json_schema"]["name"] == REVIEW_SCHEMA_NAME
    assert body["response_format"]["json_schema"]["strict"] is True
    assert "Session of 3 photos" in body["messages"][1]["content"]


def test_a_review_that_stops_short_is_retried_once_and_stored_invalid_when_it_fails_again(tmp_path):
    # Degrade, never break: an unusable review is recorded and the session falls back to the baseline
    requests = []
    short = json.dumps(review_payload(moments=((0, 1),)))
    client = build_client([completion_body(short), completion_body(short)], requests)
    store = moment_store(tmp_path)
    session = three_frame_session()

    run = moment_reviewer(client, store).review([session])

    assert len(requests) == 2
    assert "session has 3" in requests[1]["messages"][-1]["content"]
    assert run.summary.invalid == 1
    assert run.records[0].validation_status == INVALID
    assert load_reviews(store, [session], MomentReview) == {}


def test_a_cached_session_sends_no_request_and_its_review_loads_back(tmp_path):
    requests = []
    client = build_client([completion_body(json.dumps(review_payload()))], requests)
    store = moment_store(tmp_path)
    session = three_frame_session()
    moment_reviewer(client, store).review([session])

    second = moment_reviewer(client, store).review([session])
    reviews = load_reviews(store, [session], MomentReview)

    assert len(requests) == 1
    assert second.summary.sessions_skipped_existing == 1
    assert second.summary.sessions_reviewed == 0
    assert reviews[session.session_id].moments[0].last_frame == 2
    assert reviews[session.session_id].frame_count() == 3


def test_a_transport_failure_is_counted_and_leaves_no_record(tmp_path):
    def handler(request):
        return httpx.Response(402, text="no credit")

    client = OpenRouterClient("test-key", "test/model", transport=httpx.MockTransport(handler))
    store = moment_store(tmp_path)

    run = moment_reviewer(client, store).review([three_frame_session()])

    assert run.summary.request_failed == 1
    assert run.records == []
    assert run.summary.valid == 0
    assert store.records() == []


def topic_payload(groups) -> dict:
    return {"groups": [{"frames": frames, "title": title, "about": "Two stops for food on the same street.", "why": why} for frames, title, why in groups]}


def test_a_topic_review_lets_frames_belong_to_at_most_one_group_and_may_be_empty():
    TopicReview.model_validate(topic_payload([]))
    review = TopicReview.model_validate(topic_payload([([0, 2], "Noodles and tea", ["fun"]), ([1], "Bakery stop", [])]))

    assert review.frames_used() == {0, 1, 2}
    with pytest.raises(ValidationError, match="more than one group"):
        TopicReview.model_validate(topic_payload([([0, 1], "A", []), ([1, 2], "B", [])]))
    with pytest.raises(ValidationError, match="more than once"):
        TopicReview.model_validate(topic_payload([([0, 0], "A", [])]))
    with pytest.raises(ValueError, match="outside the session"):
        review.within(2)


def test_a_topic_style_is_reviewed_trip_wide_with_its_own_schema_and_store(tmp_path):
    # Every style is a different question put to the same text, with its own cache so styles never collide
    requests = []
    client = build_client([completion_body(json.dumps(topic_payload([([0, 1], "Street food on the Bund", [])])))], requests)
    style = style_for(FOODIE_TOUR)
    store = ReviewStore.for_style(tmp_path / "llm_runs", "Trip", style)
    session = session_of(three_frames(), review_version="topic-foodie_tour-1", schema_version="topic-review-1")

    run = reviewer_for(client, store, "Trip", style).review([session])

    assert run.summary.valid == 1
    assert run.summary.style == FOODIE_TOUR
    assert run.records[0].review["groups"][0]["title"] == "Street food on the Bund"
    assert store.run_dir.name == "review-rtopic-foodie_tour-1-topic-review-1"
    body = requests[0]
    assert body["response_format"]["json_schema"]["name"] == TOPIC_SCHEMA_NAME
    assert "meals and food or drink stops" in body["messages"][0]["content"]
    assert body["messages"][1]["content"].startswith("Group this trip's photos as Foodie tour.")
    assert load_reviews(store, [session], TopicReview)[session.session_id].frames_used() == {0, 1}
