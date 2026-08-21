import pytest
from pydantic import ValidationError

from llm_pipeline.schema import ImageEvaluation, response_schema


def build_payload(**overrides) -> dict:
    payload = {
        "general_description": "A stone temple gate under warm afternoon light with visitors walking the approach path",
        "scene_setting": {"types": ["museum_landmark", "street"], "other_detail": None},
        "landmark": {"name": "Longhua Temple", "confidence_tier": "medium", "evidence": "distinctive pagoda silhouette behind the gate"},
        "notable_subjects": ["Architecture, timber temple gate", "Plant, potted cypress"],
        "focal_points": ["landmark_architecture", "humans_in_background_only"],
        "activity": {"types": ["viewing_sightseeing", "walking_exploring"], "other_detail": None},
        "environment": {"types": ["historic_district"], "other_detail": None, "specific_style": "Ming dynasty temple compound"},
        "composition": ["wide_landscape"],
        "weather": ["sunny_clear"],
        "keyword_tags": ["temple", "shanghai", "afternoon light", "pagoda"],
        "photographic_style": {"types": ["warm_toned", "vibrant_saturated"], "other_detail": None},
        "screenshot": {"is_screenshot_or_document": False, "travel_relevance": "not_applicable", "document_kind": None, "confidence": 0.95},
        "memory": {"keep_signal": "keep", "reason": "A distinctive landmark scene with story value", "confidence": 0.7},
        "representative_quality": {"score": 0.8, "reasoning": "Clear subject and strong composition"},
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

    assert evaluation.scene_setting.types == ["museum_landmark", "street"]
    assert evaluation.model_dump()["landmark"]["name"] == "Longhua Temple"


def test_every_extraction_category_is_present_on_the_schema():
    # The data-extraction categories document fixes the field set the model must return
    categories = [
        "general_description",
        "scene_setting",
        "landmark",
        "notable_subjects",
        "focal_points",
        "activity",
        "environment",
        "composition",
        "weather",
        "keyword_tags",
        "photographic_style",
    ]

    assert all(category in ImageEvaluation.model_fields for category in categories)


def test_null_values_are_valid_outcomes_not_errors():
    # The briefing mandates null over invention when evidence is insufficient
    payload = build_payload(
        landmark={"name": None, "confidence_tier": None, "evidence": None},
        notable_subjects=[],
        activity={"types": [], "other_detail": None},
        environment={"types": ["city"], "other_detail": None, "specific_style": None},
    )

    evaluation = ImageEvaluation.model_validate(payload)

    assert evaluation.landmark.name is None
    assert evaluation.notable_subjects == []
    assert evaluation.activity.types == []


def test_extra_keys_are_rejected_so_the_model_cannot_add_fields():
    payload = build_payload(people_count=3)

    with pytest.raises(ValidationError):
        ImageEvaluation.model_validate(payload)


def test_a_value_outside_a_closed_list_is_rejected():
    payload = build_payload(scene_setting={"types": ["rooftop_bar"], "other_detail": None})

    with pytest.raises(ValidationError):
        ImageEvaluation.model_validate(payload)


def test_selecting_other_without_saying_what_it_was_is_rejected():
    # An unexplained other turns a closed list into an unusable free-text field
    payload = build_payload(scene_setting={"types": ["other"], "other_detail": None})

    with pytest.raises(ValidationError):
        ImageEvaluation.model_validate(payload)


def test_selecting_other_with_a_detail_is_accepted():
    payload = build_payload(scene_setting={"types": ["other"], "other_detail": "hot spring bathhouse"})

    evaluation = ImageEvaluation.model_validate(payload)

    assert evaluation.scene_setting.other_detail == "hot spring bathhouse"


def test_confidence_above_one_is_rejected_rather_than_clamped():
    payload = build_payload(representative_quality={"score": 1.4, "reasoning": "composition"})

    with pytest.raises(ValidationError):
        ImageEvaluation.model_validate(payload)


def test_a_landmark_name_without_support_is_rejected():
    # Unsupported certainty is the documented landmark failure mode
    payload = build_payload(landmark={"name": "Eiffel Tower", "confidence_tier": "high", "evidence": None})

    with pytest.raises(ValidationError):
        ImageEvaluation.model_validate(payload)


def test_more_than_four_notable_subjects_is_rejected():
    payload = build_payload(notable_subjects=["Dish, one", "Dish, two", "Dish, three", "Dish, four", "Dish, five"])

    with pytest.raises(ValidationError):
        ImageEvaluation.model_validate(payload)


def test_keyword_tags_stay_within_the_three_to_six_band():
    with pytest.raises(ValidationError):
        ImageEvaluation.model_validate(build_payload(keyword_tags=["one", "two"]))

    with pytest.raises(ValidationError):
        ImageEvaluation.model_validate(build_payload(keyword_tags=["one", "two", "three", "four", "five", "six", "seven"]))


def test_the_wire_schema_is_strict_and_free_of_bound_keywords():
    # OpenRouter strict structured outputs reject bound keywords; bounds stay enforced by pydantic re-validation
    schema = response_schema()

    nodes = collect_nodes(schema)
    object_nodes = [node for node in nodes if node.get("type") == "object"]
    assert object_nodes
    assert all(node.get("additionalProperties") is False for node in object_nodes)
    assert all("minimum" not in node and "maximum" not in node for node in nodes)
    assert all("exclusiveMinimum" not in node and "exclusiveMaximum" not in node for node in nodes)
    assert all("minItems" not in node and "maxItems" not in node for node in nodes)


def test_a_general_description_outside_the_word_band_is_rejected():
    # The category is sized to be read on its own, so a two word answer is not a usable description
    with pytest.raises(ValidationError):
        ImageEvaluation.model_validate(build_payload(general_description="A gate"))
