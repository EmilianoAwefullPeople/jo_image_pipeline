import hashlib
from datetime import datetime, timedelta, timezone

from jo_pipeline.assets import AssetSignals
from jo_pipeline.group import MomentGrouper, haversine_metres

BASE_TIME = datetime(2024, 7, 23, 12, 0, tzinfo=timezone.utc)
FLAT_HASH = "0000000000000000"
NEAR_HASH = "0000000000000003"


def distinct_hash(path):
    return hashlib.md5(path.encode()).hexdigest()[:16]


def signal(path, minutes=None, seconds=0, latitude=None, longitude=None, blur=100.0, hash_value=None, sha256=None):
    captured = BASE_TIME + timedelta(minutes=minutes, seconds=seconds) if minutes is not None else None
    return AssetSignals(
        relative_path=path,
        sha256=sha256 or path,
        captured_utc=captured,
        latitude=latitude,
        longitude=longitude,
        blur_score=blur,
        difference_hash=hash_value or distinct_hash(path),
    )


def membership_for(proposal, path):
    return next(member.membership for member in proposal.members if member.relative_path == path)


def test_photos_inside_the_window_stay_in_one_group():
    proposals = MomentGrouper().group([signal("a", 0), signal("b", 10), signal("c", 20)])

    assert len(proposals) == 1
    assert len(proposals[0].members) == 3


def test_a_gap_beyond_the_maximum_window_starts_a_new_group():
    proposals = MomentGrouper().group([signal("a", 0), signal("b", 60)])

    assert len(proposals) == 2


def test_a_fresh_group_uses_the_generous_window():
    proposals = MomentGrouper().group([signal("a", 0), signal("b", 20)])

    assert len(proposals) == 1


def test_dense_cadence_narrows_the_window_so_the_same_gap_splits():
    dense = [signal("a", 0), signal("b", 1), signal("c", 2), signal("d", 3)]
    proposals = MomentGrouper().group(dense + [signal("e", 23)])

    assert len(proposals) == 2
    assert len(proposals[0].members) == 4


def test_moving_beyond_the_place_threshold_splits_the_group():
    proposals = MomentGrouper().group([
        signal("a", 0, latitude=37.645, longitude=21.319),
        signal("b", 5, latitude=37.647, longitude=21.319),
    ])

    assert len(proposals) == 2


def test_staying_within_the_place_threshold_keeps_one_group():
    proposals = MomentGrouper().group([
        signal("a", 0, latitude=37.645, longitude=21.319),
        signal("b", 5, latitude=37.6455, longitude=21.319),
    ])

    assert len(proposals) == 1


def test_an_image_without_gps_joins_the_time_adjacent_group():
    proposals = MomentGrouper().group([
        signal("a", 0, latitude=37.645, longitude=21.319),
        signal("b", 5),
        signal("c", 10, latitude=37.6451, longitude=21.319),
    ])

    assert len(proposals) == 1
    assert proposals[0].evidence["unlocated_members"] == 1


def test_exact_duplicates_collapse_and_keep_the_membership_link():
    proposals = MomentGrouper().group([
        signal("original", 0, sha256="same"),
        signal("copy", 0, sha256="same"),
    ])

    assert len(proposals) == 1
    assert membership_for(proposals[0], "copy") == "duplicate"
    assert proposals[0].evidence["primary_count"] == 1


def test_rapid_similar_frames_are_recorded_as_a_burst():
    proposals = MomentGrouper().group([
        signal("first", 0, hash_value=FLAT_HASH),
        signal("second", 0, seconds=2, hash_value=NEAR_HASH),
    ])

    assert membership_for(proposals[0], "second") == "burst"
    assert proposals[0].members[1].evidence["hash_distance"] == 2


def test_similar_frames_outside_the_burst_window_are_duplicates():
    proposals = MomentGrouper().group([
        signal("first", 0, hash_value=FLAT_HASH),
        signal("second", 1, hash_value=NEAR_HASH),
    ])

    assert membership_for(proposals[0], "second") == "duplicate"


def test_assets_without_a_timestamp_are_proposed_separately():
    proposals = MomentGrouper().group([signal("timed", 0), signal("undated")])

    unanchored = proposals[-1]
    assert unanchored.start_utc is None
    assert unanchored.score == 0.0
    assert unanchored.evidence["reason"].startswith("no capture timestamp")


def test_the_sharpest_member_becomes_the_representative():
    proposals = MomentGrouper().group([
        signal("soft", 0, blur=50.0),
        signal("sharp", 5, blur=900.0, hash_value=FLAT_HASH),
    ])

    assert membership_for(proposals[0], "sharp") == "representative"
    assert membership_for(proposals[0], "soft") == "member"


def test_location_support_is_recorded_in_the_score_components():
    proposals = MomentGrouper().group([
        signal("a", 0, latitude=37.645, longitude=21.319),
        signal("b", 5),
    ])

    assert proposals[0].evidence["score_components"]["location_support"] == 0.5


def test_a_group_records_why_its_photos_were_held_together():
    # The demo has to explain a proposal, not just assert it
    proposals = MomentGrouper().group([
        signal("a", 0, latitude=37.645, longitude=21.319),
        signal("b", 10, latitude=37.6451, longitude=21.319),
        signal("c", 20, latitude=37.6452, longitude=21.319),
    ])

    evidence = proposals[0].evidence
    assert evidence["closest_call"]["gap_seconds"] == 600.0
    assert evidence["closest_call"]["window_seconds"] == 2700.0
    assert evidence["place_threshold_metres"] == 150.0
    assert evidence["max_distance_metres"] < 150.0
    assert evidence["opened_by"] is None
    assert evidence["closed_by"] is None


def test_a_time_split_is_recorded_on_both_sides_of_the_boundary():
    # A viewer needs the reason a moment ended, and the next one names the same cause
    proposals = MomentGrouper().group([signal("a", 0), signal("b", 60)])

    closed = proposals[0].evidence["closed_by"]
    opened = proposals[1].evidence["opened_by"]
    assert closed == opened
    assert closed["kind"] == "time_gap"
    assert closed["after"] == "a"
    assert closed["gap_seconds"] == 3600.0
    assert closed["window_seconds"] == 2700.0


def test_a_place_split_records_the_distance_that_caused_it():
    proposals = MomentGrouper().group([
        signal("a", 0, latitude=37.645, longitude=21.319),
        signal("b", 5, latitude=37.647, longitude=21.319),
    ])

    boundary = proposals[0].evidence["closed_by"]
    assert boundary["kind"] == "place_change"
    assert boundary["after"] == "a"
    assert boundary["distance_metres"] > 150.0
    assert boundary["threshold_metres"] == 150.0


def test_the_reported_gap_is_the_one_that_came_nearest_to_splitting_the_moment():
    # Equal gaps are not equally close to a boundary once the window has narrowed
    dense = [signal("a", 0), signal("b", 1), signal("c", 2), signal("d", 3)]
    proposals = MomentGrouper().group(dense + [signal("e", 23)])

    closest = proposals[0].evidence["closest_call"]
    assert closest["gap_seconds"] == 60.0
    assert closest["window_seconds"] == 900.0
    assert proposals[0].evidence["closed_by"]["window_seconds"] == 900.0


def test_haversine_measures_a_known_separation():
    start = signal("a", 0, latitude=0.0, longitude=0.0)
    end = signal("b", 0, latitude=0.0, longitude=0.001)

    assert 110 < haversine_metres(start, end) < 112


def test_haversine_is_unavailable_without_coordinates():
    assert haversine_metres(signal("a", 0), signal("b", 0)) is None
