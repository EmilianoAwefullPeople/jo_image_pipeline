import pytest

from llm_pipeline.prompts import default_topic_prompts
from llm_pipeline.styles import MOMENT_REVIEW, MOMENTS, OUTING, SELECTION, STYLES, TOPIC_REVIEW, TRIP, style_for


def test_every_style_is_a_complete_fixed_question_and_moments_is_the_only_outing_review():
    # The page offers a restricted list: each entry has to say what it asks and how it is run
    assert list(STYLES)[0] == MOMENTS
    for style in STYLES.values():
        assert style.name and style.description and len(style.instructions) > 40
        assert style.kind in (MOMENT_REVIEW, TOPIC_REVIEW)
    assert STYLES[MOMENTS].kind == MOMENT_REVIEW and STYLES[MOMENTS].scope == OUTING
    topic = [style for style in STYLES.values() if style.kind == TOPIC_REVIEW]
    assert len(topic) == 5
    assert all(style.scope == TRIP and style.coverage == SELECTION for style in topic)


def test_an_unknown_style_is_refused_by_name():
    with pytest.raises(ValueError, match="mystery"):
        style_for("mystery")


def test_topic_prompts_carry_the_style_instructions_and_a_style_specific_version():
    prompts = default_topic_prompts(style_for("landmark_tour"))

    assert prompts.version == "topic-landmark_tour-1"
    assert "one group per named landmark" in prompts.system
    assert "Never name a place, landmark, subject, dish or activity the frame text does not name" in prompts.system
    messages = prompts.build_messages("Session of 2 photos")
    assert messages[1]["content"] == "Group this trip's photos as Landmark tour.\n\nSession of 2 photos"
    assert default_topic_prompts(style_for("memories")).version != prompts.version
