from jo_pipeline.group import BURST, GROUPER_VERSION, MEMBER, REPRESENTATIVE, GroupMember, GroupProposal
from jo_pipeline.refine import REFINER_VERSION, ProposalRefiner, build_image_signals, build_llm_observations


def evaluation(keep="keep", quality=0.5, screenshot=False, relevance="not_applicable", reason="a reason") -> dict:
    return {
        "general_description": "A quiet row of shopfronts along a narrow street in the late afternoon",
        "scene_setting": {"types": ["street"], "other_detail": None},
        "landmark": {"name": None, "confidence_tier": None, "evidence": None},
        "notable_subjects": [],
        "focal_points": ["no_clear_subject"],
        "activity": {"types": [], "other_detail": None},
        "environment": {"types": ["city"], "other_detail": None, "specific_style": None},
        "composition": ["wide_landscape"],
        "weather": ["overcast"],
        "keyword_tags": ["street", "shopfronts", "afternoon"],
        "photographic_style": {"types": ["muted_desaturated"], "other_detail": None},
        "screenshot": {"is_screenshot_or_document": screenshot, "travel_relevance": relevance, "document_kind": None, "confidence": 0.9},
        "memory": {"keep_signal": keep, "reason": reason, "confidence": 0.77},
        "representative_quality": {"score": quality, "reasoning": "composition and story"},
    }


def proposal(members: list[GroupMember], score=0.5) -> GroupProposal:
    return GroupProposal(
        label="2024-04-18 20:12 to 20:17 UTC",
        start_utc="2024-04-18T20:12:00+00:00",
        end_utc="2024-04-18T20:17:00+00:00",
        score=score,
        method_version=GROUPER_VERSION,
        members=members,
        evidence={"member_count": len(members), "score_components": {"cohesion": 0.9, "location_support": 1.0, "visual_quality": 0.2}},
    )


def test_a_leave_out_photo_is_dropped_from_its_group_with_the_reason_recorded():
    # The Week 2 gap: photos the traveler deliberately excluded were grouped anyway
    members = [GroupMember("a.jpg", REPRESENTATIVE, {"blur_score": 900.0}), GroupMember("b.jpg", MEMBER)]
    signals = build_image_signals({"a.jpg": evaluation(quality=0.9), "b.jpg": evaluation(keep="leave_out", reason="a blurred accident")})

    refined = ProposalRefiner().refine([proposal(members)], signals)

    assert [member.relative_path for member in refined[0].members] == ["a.jpg"]
    excluded = refined[0].evidence["excluded_by_signal"]
    assert excluded[0]["relative_path"] == "b.jpg"
    assert excluded[0]["reason"] == "a blurred accident"
    assert excluded[0]["confidence"] == 0.77
    assert refined[0].evidence["member_count"] == 1


def test_the_representative_is_re_elected_on_model_quality_not_sharpness():
    # The schema scores representative quality on composition and story, explicitly not sharpness
    members = [GroupMember("sharp.jpg", REPRESENTATIVE, {"reason": "sharpest member", "blur_score": 2000.0}), GroupMember("story.jpg", MEMBER)]
    signals = build_image_signals({"sharp.jpg": evaluation(quality=0.2), "story.jpg": evaluation(quality=0.95)})

    refined = ProposalRefiner().refine([proposal(members)], signals)

    elected = [member for member in refined[0].members if member.membership == REPRESENTATIVE]
    assert len(elected) == 1
    assert elected[0].relative_path == "story.jpg"
    assert elected[0].evidence["previous_representative"] == "sharp.jpg"
    assert elected[0].evidence["representative_score"] == 0.95
    superseded = [member for member in refined[0].members if member.relative_path == "sharp.jpg"]
    assert superseded[0].membership == MEMBER


def test_promoting_a_burst_frame_keeps_the_link_to_the_frame_it_came_from():
    # The proposal still counts it as an attached frame, so the link has to survive the promotion
    members = [
        GroupMember("first.jpg", REPRESENTATIVE, {"reason": "sharpest member", "blur_score": 2000.0}),
        GroupMember("second.jpg", BURST, {"canonical": "first.jpg", "hash_distance": 3, "gap_seconds": 2.0}),
    ]
    signals = build_image_signals({"first.jpg": evaluation(quality=0.2), "second.jpg": evaluation(quality=0.95)})

    refined = ProposalRefiner().refine([proposal(members)], signals)

    elected = [member for member in refined[0].members if member.membership == REPRESENTATIVE]
    assert elected[0].relative_path == "second.jpg"
    assert elected[0].evidence["canonical"] == "first.jpg"
    assert elected[0].evidence["gap_seconds"] == 2.0
    assert elected[0].evidence["reason"] == "highest model representative quality"


def test_a_proposal_whose_every_member_is_leave_out_is_dropped_entirely():
    members = [GroupMember("a.jpg", REPRESENTATIVE), GroupMember("b.jpg", MEMBER)]
    signals = build_image_signals({"a.jpg": evaluation(keep="leave_out"), "b.jpg": evaluation(keep="leave_out")})

    refined = ProposalRefiner().refine([proposal(members)], signals)

    assert refined == []


def test_a_non_travel_relevant_screenshot_is_flagged_but_never_dropped():
    # The brief requires screenshots be flagged for review, never deletion
    members = [GroupMember("shot.png", REPRESENTATIVE), GroupMember("real.jpg", MEMBER)]
    signals = build_image_signals({
        "shot.png": evaluation(quality=0.1, screenshot=True, relevance="not_travel_relevant"),
        "real.jpg": evaluation(quality=0.8),
    })

    refined = ProposalRefiner().refine([proposal(members)], signals)

    assert [member.relative_path for member in refined[0].members] == ["shot.png", "real.jpg"]
    assert refined[0].evidence["screenshots_flagged"] == [{"relative_path": "shot.png", "scene_setting_types": ["street"]}]
    assert "excluded_by_signal" not in refined[0].evidence


def test_the_refined_score_replaces_the_blur_proxy_with_model_quality():
    members = [GroupMember("a.jpg", REPRESENTATIVE), GroupMember("b.jpg", MEMBER)]
    signals = build_image_signals({"a.jpg": evaluation(quality=1.0), "b.jpg": evaluation(quality=0.6)})

    refined = ProposalRefiner().refine([proposal(members, score=0.44)], signals)

    components = refined[0].evidence["score_components"]
    assert components["model_quality"] == 0.8
    assert components["visual_quality"] == 0.2
    assert refined[0].score == round(0.4 * 0.9 + 0.3 * 1.0 + 0.3 * 0.8, 3)
    assert refined[0].evidence["baseline_score"] == 0.44
    assert refined[0].method_version == REFINER_VERSION
    assert refined[0].evidence["baseline_method_version"] == GROUPER_VERSION


def test_refinement_without_any_signal_is_a_no_op():
    # A dataset with no evaluations must still return the deterministic baseline untouched
    members = [GroupMember("a.jpg", REPRESENTATIVE), GroupMember("b.jpg", BURST, {"canonical": "a.jpg"})]
    baseline = [proposal(members)]

    refined = ProposalRefiner().refine(baseline, {})

    assert refined == baseline


def test_a_member_with_no_evaluation_is_kept_and_never_elected_representative():
    # Partial coverage must not silently drop photos the model never saw
    members = [GroupMember("seen.jpg", MEMBER), GroupMember("unseen.jpg", REPRESENTATIVE)]
    signals = build_image_signals({"seen.jpg": evaluation(quality=0.4)})

    refined = ProposalRefiner().refine([proposal(members)], signals)

    assert [member.relative_path for member in refined[0].members] == ["seen.jpg", "unseen.jpg"]
    elected = [member for member in refined[0].members if member.membership == REPRESENTATIVE]
    assert elected[0].relative_path == "seen.jpg"


def test_an_evaluation_that_failed_validation_yields_no_signal():
    signals = build_image_signals({"a.jpg": None, "b.jpg": evaluation()})

    assert list(signals) == ["b.jpg"]


def test_every_extraction_category_reaches_the_observation_rows():
    # The data-extraction categories document is the contract for what the model returns and what gets persisted
    observations = build_llm_observations(evaluation(), "openai/gpt-5.6-sol", "p2/llm-eval-2")

    fields = [observation.field for observation in observations]
    assert fields == [
        "general_description",
        "scene_setting_types",
        "landmark_candidate",
        "notable_subjects",
        "focal_point_types",
        "activity_types",
        "environment_general_types",
        "environment_specific_style",
        "composition_types",
        "weather_conditions",
        "keyword_tags",
        "photographic_style_types",
        "is_screenshot_or_document",
        "memory_keep_signal",
        "representative_quality",
    ]


def test_a_category_the_model_left_empty_is_recorded_as_unknown_rather_than_dropped():
    observations = {observation.field: observation for observation in build_llm_observations(evaluation(), "m", "v")}

    assert observations["activity_types"].value is None
    assert observations["activity_types"].unknown_reason is not None
    assert observations["scene_setting_types"].value == ["street"]
    assert observations["scene_setting_types"].unknown_reason is None
