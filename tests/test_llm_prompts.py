from llm_pipeline.prompts import CUSTOM_PROMPT_VERSION, PROMPT_VERSION, custom_prompts, default_prompts, template_reject_reason


def test_the_supplied_prompt_text_is_what_reaches_the_model():
    # A front end edit is pointless if the stored prompt is sent anyway
    prompts = custom_prompts("Only answer in Latin.", "Photo taken at {capture_local_time}.")

    messages = prompts.build_messages("data:image/jpeg;base64,abc", "2026-05-04 09:30:00")

    assert messages[0]["content"] == "Only answer in Latin."
    assert messages[1]["content"][0]["image_url"]["url"] == "data:image/jpeg;base64,abc"
    assert messages[1]["content"][1]["text"] == "Photo taken at 2026-05-04 09:30:00."
    assert prompts.version == CUSTOM_PROMPT_VERSION


def test_a_missing_capture_time_is_named_rather_than_left_blank():
    prompts = custom_prompts("System", "Taken at {capture_local_time}.")

    messages = prompts.build_messages("data:image/jpeg;base64,abc", None)

    assert messages[1]["content"][1]["text"] == "Taken at unknown."


def test_the_default_prompt_carries_the_stored_version():
    prompts = default_prompts()

    assert prompts.version == PROMPT_VERSION
    assert "{capture_local_time}" in prompts.user_template
    assert prompts.system.strip()


def test_a_template_that_cannot_be_formatted_is_reported_not_raised():
    # An unknown brace would otherwise fail mid run, one image at a time
    assert template_reject_reason("Taken at {capture_local_time}.") is None
    assert template_reject_reason("No placeholders at all.") is None
    assert "KeyError" in template_reject_reason("Taken at {when}.")
    assert template_reject_reason("An unbalanced { brace.") is not None
