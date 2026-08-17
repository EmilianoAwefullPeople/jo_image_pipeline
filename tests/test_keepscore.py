from jo_pipeline.keepscore import KEEPSCORE_VERSION, KeepSignalScorer
from jo_pipeline.reference import ReferenceGroup, ReferenceGrouping
from jo_pipeline.refine import build_image_signals
from tests.test_refine import evaluation


def grouping(grouped: list, excluded: list) -> ReferenceGrouping:
    return ReferenceGrouping(
        dataset_id="Shanghai",
        method_version="reference-1",
        groups=[ReferenceGroup(index=1, asset_paths=grouped, unmatched_media=[])],
        excluded_paths=excluded,
        unmatched_media=[],
    )


def test_the_keep_signal_is_scored_against_the_photos_the_traveler_left_out():
    # This is the specific Week 2 gap the signal exists to close
    reference = grouping(grouped=["keep1.jpg", "keep2.jpg"], excluded=["out1.jpg", "out2.jpg"])
    signals = build_image_signals({
        "keep1.jpg": evaluation(keep="keep"),
        "keep2.jpg": evaluation(keep="unsure"),
        "out1.jpg": evaluation(keep="leave_out", reason="a duplicate of a better frame"),
        "out2.jpg": evaluation(keep="keep"),
    })

    score = KeepSignalScorer().score(reference, signals)

    assert score.excluded_total == 2
    assert score.excluded_with_signal == 2
    assert score.excluded_caught == 1
    assert score.recall == 0.5
    assert score.grouped_false_positives == 0
    assert score.false_positive_rate == 0.0
    assert score.caught[0].relative_path == "out1.jpg"
    assert score.caught[0].reason == "a duplicate of a better frame"
    assert score.missed[0].relative_path == "out2.jpg"
    assert score.missed[0].keep_signal == "keep"
    assert score.method_version == KEEPSCORE_VERSION


def test_leave_out_on_a_photo_the_traveler_kept_counts_as_a_false_positive():
    reference = grouping(grouped=["keep1.jpg", "keep2.jpg"], excluded=["out1.jpg"])
    signals = build_image_signals({
        "keep1.jpg": evaluation(keep="leave_out", reason="looked incidental"),
        "keep2.jpg": evaluation(keep="keep"),
        "out1.jpg": evaluation(keep="leave_out"),
    })

    score = KeepSignalScorer().score(reference, signals)

    assert score.recall == 1.0
    assert score.grouped_with_signal == 2
    assert score.grouped_false_positives == 1
    assert score.false_positive_rate == 0.5
    assert score.false_positives[0].relative_path == "keep1.jpg"


def test_photos_never_evaluated_are_excluded_from_the_denominator():
    # Scoring must reflect what the model actually saw, not what the reference contained
    reference = grouping(grouped=["keep1.jpg"], excluded=["out1.jpg", "out2.jpg", "out3.jpg"])
    signals = build_image_signals({"out1.jpg": evaluation(keep="leave_out")})

    score = KeepSignalScorer().score(reference, signals)

    assert score.excluded_total == 3
    assert score.excluded_with_signal == 1
    assert score.excluded_caught == 1
    assert score.recall == 1.0
    assert score.grouped_with_signal == 0
    assert score.false_positive_rate == 0.0


def test_a_repeated_excluded_path_is_counted_once():
    # ReferenceReader can emit the same path twice when a photo appears in two excluded paragraphs
    reference = grouping(grouped=["keep1.jpg"], excluded=["out1.jpg", "out1.jpg"])
    signals = build_image_signals({"out1.jpg": evaluation(keep="leave_out"), "keep1.jpg": evaluation(keep="keep")})

    score = KeepSignalScorer().score(reference, signals)

    assert score.excluded_total == 1
    assert score.excluded_caught == 1


def test_the_score_serializes_with_its_per_path_detail():
    reference = grouping(grouped=["keep1.jpg"], excluded=["out1.jpg"])
    signals = build_image_signals({"out1.jpg": evaluation(keep="leave_out"), "keep1.jpg": evaluation(keep="keep")})

    payload = KeepSignalScorer().score(reference, signals).as_dict()

    assert payload["recall"] == 1.0
    assert payload["caught"] == [{"relative_path": "out1.jpg", "keep_signal": "leave_out", "reason": "a reason", "confidence": 0.77}]
    assert payload["missed"] == []
    assert payload["false_positives"] == []
