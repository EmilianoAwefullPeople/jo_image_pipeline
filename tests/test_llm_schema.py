import pytest
from pydantic import ValidationError

from llm_pipeline.schema import ImageEvaluation, response_schema


def build_payload(**overrides) -> dict:
    payload = {
        "caption": "A stone temple gate under afternoon light",
        "scene": {"scene_type": "landmark", "confidence": 0.8, "evidence": "ornate gate structure and temple signage"},
        "activity": {"description": "visiting a temple", "confidence": 0.7, "evidence": "walking path leading toward the gate"},
        "landmark": {"name": "Longhua Temple", "confidence": 0.5, "evidence": "distinctive pagoda silhouette behind the gate"},
        "visible_text": {"transcription": "Longhua Temple", "text_kind": "sign", "language": "zh", "confidence": 0.9},
        "screenshot": {"is_screenshot_or_document": False, "travel_relevance": "not_applicable", "document_kind": None, "confidence": 0.95},
        "emotions": [{"label": "awe", "confidence": 0.6}],
        "memory": {"keep_signal": "keep", "reason": "A distinctive landmark scene with story value", "confidence": 0.7},
        "representative_quality": {"score": 0.8, "reasoning": "Clear subject and strong composition"},
        "journaling_prompt": "What did it feel like to walk through this gate?",
    }
    payload.update(overrides)
    return payload


def collect_nodes(node) -> list:
    # The wire schema must be strict at every level, so tests walk every nested dict
    nodes = []
    if isinstance(node, dict):
        nodes.append(node)
        for value in node.values():
            nodes.extend(collect_nodes(value))
    if isinstance(node, list):
        for item in node:
            nodes.extend(collect_nodes(item))
    return nodes


def test_a_complete_payload_round_trips_through_the_schema():
    evaluation = ImageEvaluation.model_validate(build_payload())

    assert evaluation.scene.scene_type == "landmark"
    assert evaluation.model_dump()["landmark"]["name"] == "Longhua Temple"


def test_null_values_are_valid_outcomes_not_errors():
    # The briefing mandates null over invention when evidence is insufficient
    payload = build_payload(
        activity={"description": None, "confidence": 0.2, "evidence": None},
        landmark={"name": None, "confidence": 0.1, "evidence": None},
        visible_text={"transcription": None, "text_kind": None, "language": None, "confidence": 0.9},
        emotions=[],
        journaling_prompt=None,
    )

    evaluation = ImageEvaluation.model_validate(payload)

    assert evaluation.landmark.name is None
    assert evaluation.emotions == []


def test_extra_keys_are_rejected_so_the_model_cannot_add_fields():
    payload = build_payload(people_count=3)

    with pytest.raises(ValidationError):
        ImageEvaluation.model_validate(payload)


def test_confidence_above_one_is_rejected_rather_than_clamped():
    payload = build_payload(scene={"scene_type": "landmark", "confidence": 1.4, "evidence": "gate"})

    with pytest.raises(ValidationError):
        ImageEvaluation.model_validate(payload)


def test_a_landmark_name_without_evidence_is_rejected():
    # Unsupported certainty is the documented landmark failure mode
    payload = build_payload(landmark={"name": "Eiffel Tower", "confidence": 0.9, "evidence": None})

    with pytest.raises(ValidationError):
        ImageEvaluation.model_validate(payload)


def test_the_wire_schema_is_strict_and_free_of_bound_keywords():
    # OpenRouter strict structured outputs reject numeric bound keywords; bounds stay enforced by pydantic re-validation
    schema = response_schema()

    nodes = collect_nodes(schema)
    object_nodes = [node for node in nodes if node.get("type") == "object"]
    assert object_nodes
    assert all(node.get("additionalProperties") is False for node in object_nodes)
    assert all("minimum" not in node and "maximum" not in node for node in nodes)
    assert all("exclusiveMinimum" not in node and "exclusiveMaximum" not in node for node in nodes)
