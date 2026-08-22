import json

import httpx
import pytest

from llm_pipeline.client import OpenRouterClient, OpenRouterError
from llm_pipeline.evaluate import INVALID, VALID, image_evaluator


def build_payload() -> dict:
    return {
        "general_description": "A stone temple gate under warm afternoon light at the end of a quiet approach",
        "scene_setting": {"types": ["museum_landmark"], "other_detail": None},
        "landmark": {"name": None, "confidence_tier": None, "evidence": None},
        "notable_subjects": ["Architecture, timber temple gate"],
        "focal_points": ["landmark_architecture"],
        "activity": {"types": ["viewing_sightseeing"], "other_detail": None},
        "environment": {"types": ["historic_district"], "other_detail": None, "specific_style": "temple compound"},
        "composition": ["wide_landscape"],
        "weather": ["sunny_clear"],
        "keyword_tags": ["temple", "gate", "afternoon light"],
        "photographic_style": {"types": ["warm_toned"], "other_detail": None},
        "screenshot": {"is_screenshot_or_document": False, "travel_relevance": "not_applicable", "document_kind": None, "confidence": 0.95},
        "memory": {"keep_signal": "keep", "reason": "A distinctive scene with story value", "confidence": 0.7},
        "representative_quality": {"score": 0.8, "reasoning": "Clear subject and strong composition"},
    }


def completion_body(content, cost=0.01) -> dict:
    return {
        "id": "gen-abc",
        "choices": [{"message": {"role": "assistant", "content": content}}],
        "usage": {"prompt_tokens": 900, "completion_tokens": 400, "cost": cost},
    }


def build_client(bodies, requests) -> OpenRouterClient:
    def handler(request):
        requests.append(json.loads(request.content.decode()))
        return httpx.Response(200, json=bodies[len(requests) - 1])

    return OpenRouterClient("test-key", "test/model", transport=httpx.MockTransport(handler))


def build_messages_stub() -> list[dict]:
    return [{"role": "system", "content": "system"}, {"role": "user", "content": [{"type": "text", "text": "evaluate"}]}]


def test_a_valid_first_response_completes_in_one_attempt_with_usage_captured():
    requests = []
    client = build_client([completion_body(json.dumps(build_payload()))], requests)

    outcome = image_evaluator(client).evaluate("IMG_0001.jpg", build_messages_stub())

    assert outcome.validation_status == VALID
    assert outcome.attempts == 1
    assert outcome.cost_usd == 0.01
    assert outcome.prompt_tokens == 900
    assert outcome.evaluation.memory.keep_signal == "keep"


def test_the_request_pins_the_model_route_and_demands_strict_schema_output():
    # The briefing forbids hidden fallbacks and requires constrained structured output
    requests = []
    client = build_client([completion_body(json.dumps(build_payload()))], requests)

    image_evaluator(client).evaluate("IMG_0001.jpg", build_messages_stub())

    body = requests[0]
    assert body["model"] == "test/model"
    assert body["provider"] == {"allow_fallbacks": False, "data_collection": "deny"}
    assert body["response_format"]["json_schema"]["strict"] is True
    assert body["usage"] == {"include": True}


def test_an_invalid_response_is_retried_once_with_the_validation_error():
    requests = []
    client = build_client([completion_body("not json"), completion_body(json.dumps(build_payload()), cost=0.02)], requests)

    outcome = image_evaluator(client).evaluate("IMG_0001.jpg", build_messages_stub())

    assert outcome.validation_status == VALID
    assert outcome.attempts == 2
    assert outcome.cost_usd == 0.03
    retry_messages = requests[1]["messages"]
    assert retry_messages[-2] == {"role": "assistant", "content": "not json"}
    assert "failed validation" in retry_messages[-1]["content"]


def test_a_second_invalid_response_is_stored_for_review_without_a_third_attempt():
    requests = []
    client = build_client([completion_body("not json"), completion_body('{"still": "wrong"}')], requests)

    outcome = image_evaluator(client).evaluate("IMG_0001.jpg", build_messages_stub())

    assert outcome.validation_status == INVALID
    assert outcome.attempts == 2
    assert len(requests) == 2
    assert "attempt 1" in outcome.failure_detail
    assert "attempt 2" in outcome.failure_detail
    assert outcome.evaluation is None


def test_a_transport_failure_raises_visibly_rather_than_falling_back():
    def handler(request):
        return httpx.Response(500, text="provider exploded")

    client = OpenRouterClient("test-key", "test/model", transport=httpx.MockTransport(handler))

    with pytest.raises(OpenRouterError) as failure:
        image_evaluator(client).evaluate("IMG_0001.jpg", build_messages_stub())

    assert failure.value.status_code == 500
    assert "provider exploded" in failure.value.body
