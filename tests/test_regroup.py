from jo_pipeline.group import GROUPER_VERSION, MomentGrouper
from jo_pipeline.refine import build_image_signals
from jo_pipeline.regroup import FALLBACK, MERGED, REGROUP_VERSION, REVIEW_SPLIT, SESSION_GAP_SECONDS, SESSION_MAX_FRAMES, SKIPPED, SPLIT, UNCHANGED, RegroupApplier, Regrouper, SessionBuilder
from llm_pipeline.schema import MomentReview
from tests.test_group import FLAT_HASH, NEAR_HASH, signal
from tests.test_refine import evaluation


def signals_by_path(assets):
    return {asset.relative_path: asset for asset in assets}


def image_signals(paths, **overrides):
    return build_image_signals("llm-eval-2", {path: evaluation(**overrides.get(path, {})) for path in paths})


def review(moments, leave_out=()):
    return MomentReview.model_validate({
        "moments": [{"first_frame": first, "last_frame": last, "title": f"Moment {first}", "reason": f"frames {first} to {last} share a place"} for first, last in moments],
        "leave_out": [{"frame": index, "reason": "a practical screenshot", "confidence": 0.9} for index in leave_out],
    })


def two_proposals_in_one_session():
    # a, b form one baseline proposal; c sits 13 minutes and 180 m on, so the place rule opens a second proposal
    assets = [signal("a", 0, latitude=31.0, longitude=121.0), signal("b", 1, latitude=31.0, longitude=121.0), signal("c", 14, latitude=31.0, longitude=121.0019)]
    baseline = MomentGrouper().group(assets)
    assert len(baseline) == 2
    return assets, baseline


def test_sessions_follow_the_day_and_the_review_frames_carry_gaps_distances_and_boundaries():
    # A session is one outing: proposals within eight hours of each other, with the baseline boundary marked between them
    assets, baseline = two_proposals_in_one_session()
    next_day = [signal("d", 24 * 60), signal("e", 24 * 60 + 5)]
    baseline = MomentGrouper().group(assets + next_day)

    sessions = SessionBuilder().build("Trip", "p2-llm-eval-2", baseline, signals_by_path(assets + next_day), image_signals(["a", "b", "c", "d", "e"]))

    assert [len(session.frames) for session in sessions] == [3, 2]
    frames = sessions[0].frames
    assert [frame.index for frame in frames] == [0, 1, 2]
    assert frames[0].gap_seconds is None and frames[0].boundary_before is None
    assert frames[1].gap_seconds == 60.0 and frames[1].boundary_before is None
    assert frames[2].boundary_before["kind"] == "place_change"
    assert 170 < frames[2].distance_metres < 190
    assert frames[2].summary.startswith("A quiet row of shopfronts")
    assert sessions[0].source_version == "p2-llm-eval-2"


def test_a_single_photo_outing_is_not_reviewed_and_an_oversize_outing_is_cut_between_proposals():
    assets = [signal("lone", 0)]
    crowded = [signal(f"p{index}", 5 * 24 * 60 + index * 2) for index in range(SESSION_MAX_FRAMES + 6)]
    late = [signal("late", 5 * 24 * 60 + 200)]
    everything = assets + crowded + late
    baseline = MomentGrouper().group(everything)

    sessions = SessionBuilder().build("Trip", "p2-llm-eval-2", baseline, signals_by_path(everything), image_signals([asset.relative_path for asset in everything]))

    # The oversize outing is cut only at its one inter-proposal gap, which leaves the 46-photo proposal whole and the lone late photo unreviewed
    reviewed = {frame.relative_path for session in sessions for frame in session.frames}
    assert all(len(session.frames) >= 2 for session in sessions)
    assert "lone" not in reviewed and "late" not in reviewed
    assert len(sessions) == 1
    assert len(sessions[0].frames) == SESSION_MAX_FRAMES + 6
    assert reviewed == {asset.relative_path for asset in crowded}


def test_a_gap_beyond_the_session_ceiling_starts_a_new_session():
    assets = [signal("a", 0), signal("b", 5), signal("c", SESSION_GAP_SECONDS // 60 + 10), signal("d", SESSION_GAP_SECONDS // 60 + 12)]
    baseline = MomentGrouper().group(assets)

    sessions = SessionBuilder().build("Trip", "p2-llm-eval-2", baseline, signals_by_path(assets), image_signals(["a", "b", "c", "d"]))

    assert [[frame.relative_path for frame in session.frames] for session in sessions] == [["a", "b"], ["c", "d"]]


def test_a_review_that_joins_two_baseline_proposals_yields_one_moment_with_the_bridged_boundary_recorded():
    assets, baseline = two_proposals_in_one_session()
    sessions = SessionBuilder().build("Trip", "p2-llm-eval-2", baseline, signals_by_path(assets), image_signals(["a", "b", "c"]))
    reviews = {sessions[0].session_id: review([(0, 2)])}

    regrouped = RegroupApplier().apply(baseline, signals_by_path(assets), sessions, reviews)

    assert len(regrouped) == 1
    assert [member.relative_path for member in regrouped[0].members] == ["a", "b", "c"]
    assert regrouped[0].evidence["review"]["title"] == "Moment 0"
    assert regrouped[0].evidence["review"]["change"] == MERGED
    assert regrouped[0].evidence["review"]["baseline_labels"] == [baseline[0].label, baseline[1].label]
    assert regrouped[0].evidence["bridged_boundaries"][0]["kind"] == "place_change"
    assert regrouped[0].evidence["opened_by"] is None
    assert regrouped[0].evidence["closed_by"] is None
    assert regrouped[0].start_utc == baseline[0].start_utc
    assert regrouped[0].end_utc == baseline[1].end_utc
    assert regrouped[0].method_version == GROUPER_VERSION


def test_a_review_that_splits_a_baseline_proposal_records_the_new_boundary_on_both_sides():
    assets = [signal("a", 0), signal("b", 2), signal("c", 4)]
    baseline = MomentGrouper().group(assets)
    sessions = SessionBuilder().build("Trip", "p2-llm-eval-2", baseline, signals_by_path(assets), image_signals(["a", "b", "c"]))
    reviews = {sessions[0].session_id: review([(0, 0), (1, 2)])}

    regrouped = RegroupApplier().apply(baseline, signals_by_path(assets), sessions, reviews)

    assert [[member.relative_path for member in proposal.members] for proposal in regrouped] == [["a"], ["b", "c"]]
    assert regrouped[0].evidence["review"]["change"] == SPLIT
    assert regrouped[0].evidence["closed_by"]["kind"] == REVIEW_SPLIT
    assert regrouped[0].evidence["closed_by"]["after"] == "a"
    assert regrouped[1].evidence["opened_by"] == regrouped[0].evidence["closed_by"]
    assert regrouped[1].evidence["review"]["change"] == SPLIT


def test_an_unchanged_moment_is_marked_as_such_and_a_session_without_a_review_passes_through():
    assets, baseline = two_proposals_in_one_session()
    sessions = SessionBuilder().build("Trip", "p2-llm-eval-2", baseline, signals_by_path(assets), image_signals(["a", "b", "c"]))

    confirmed = RegroupApplier().apply(baseline, signals_by_path(assets), sessions, {sessions[0].session_id: review([(0, 1), (2, 2)])})
    fallback = RegroupApplier().apply(baseline, signals_by_path(assets), sessions, {})

    assert [proposal.evidence["review"]["change"] for proposal in confirmed] == [UNCHANGED, UNCHANGED]
    assert confirmed[0].evidence["closed_by"] == baseline[0].evidence["closed_by"]
    assert [proposal.members for proposal in fallback] == [proposal.members for proposal in baseline]
    assert fallback[0].evidence["review"]["status"] == FALLBACK


def test_attached_duplicates_follow_their_canonical_and_the_unanchored_proposal_is_untouched():
    assets = [signal("a", 0, hash_value=FLAT_HASH), signal("a_copy", 0, seconds=1, hash_value=NEAR_HASH), signal("b", 20), signal("nowhere", None)]
    baseline = MomentGrouper().group(assets)
    sessions = SessionBuilder().build("Trip", "p2-llm-eval-2", baseline, signals_by_path(assets), image_signals(["a", "a_copy", "b", "nowhere"]))
    assert [frame.relative_path for frame in sessions[0].frames] == ["a", "b"]
    reviews = {sessions[0].session_id: review([(0, 0), (1, 1)])}

    regrouped = RegroupApplier().apply(baseline, signals_by_path(assets), sessions, reviews)

    memberships = {member.relative_path: member.membership for member in regrouped[0].members}
    assert memberships["a_copy"] == "burst"
    assert regrouped[-1].label == "Unanchored assets"
    assert regrouped[-1].evidence["review"]["status"] == SKIPPED


def test_review_leave_outs_are_dropped_through_refinement_with_their_source_recorded():
    # A set-level exclusion uses the same reversible mechanism as the per-image one, so the traveller can put it back
    assets, baseline = two_proposals_in_one_session()
    signals = image_signals(["a", "b", "c"])
    sessions = SessionBuilder().build("Trip", "p2-llm-eval-2", baseline, signals_by_path(assets), signals)
    reviews = {sessions[0].session_id: review([(0, 2)], leave_out=(1,))}

    regrouped = Regrouper().regroup(baseline, signals_by_path(assets), signals, sessions, reviews)
    review_signals = Regrouper().review_signals(signals, sessions, reviews)

    assert [member.relative_path for member in regrouped[0].members] == ["a", "c"]
    excluded = regrouped[0].evidence["excluded_by_signal"]
    assert excluded == [{"relative_path": "b", "membership": "member", "reason": "a practical screenshot", "confidence": 0.9, "source": "moment_review"}]
    assert regrouped[0].method_version == REGROUP_VERSION
    assert regrouped[0].evidence["baseline_method_version"] == GROUPER_VERSION
    assert review_signals["b"].keep_signal == "leave_out"
    assert review_signals["a"].keep_signal == "keep"
