from jo_pipeline.group import GroupMember, GroupProposal
from jo_pipeline.reference import ReferenceGroup, ReferenceGrouping
from jo_pipeline.review import GroupingReviewer


def proposal(paths):
    return GroupProposal(
        label="proposal",
        start_utc=None,
        end_utc=None,
        score=0.0,
        method_version="group-1",
        members=[GroupMember(path, "member") for path in paths],
    )


def reference_grouping(groups, excluded=None):
    return ReferenceGrouping(
        dataset_id="sample",
        method_version="reference-1",
        groups=[ReferenceGroup(index=index + 1, asset_paths=paths, unmatched_media=[]) for index, paths in enumerate(groups)],
        excluded_paths=excluded or [],
        unmatched_media=[],
    )


def test_identical_grouping_scores_a_perfect_pair_f1():
    proposals = [proposal(["a", "b"]), proposal(["c", "d"])]
    reference = reference_grouping([["a", "b"], ["c", "d"]])

    comparison = GroupingReviewer().compare(proposals, reference)

    assert comparison.pair_precision == 1.0
    assert comparison.pair_recall == 1.0
    assert comparison.pair_f1 == 1.0
    assert comparison.split_groups == 0
    assert comparison.merged_proposals == 0


def test_splitting_a_reference_group_costs_recall_but_not_precision():
    proposals = [proposal(["a"]), proposal(["b"])]
    reference = reference_grouping([["a", "b"]])

    comparison = GroupingReviewer().compare(proposals, reference)

    assert comparison.pair_recall == 0.0
    assert comparison.pair_precision == 0.0
    assert comparison.split_groups == 1


def test_merging_two_reference_groups_costs_precision():
    proposals = [proposal(["a", "b", "c", "d"])]
    reference = reference_grouping([["a", "b"], ["c", "d"]])

    comparison = GroupingReviewer().compare(proposals, reference)

    assert comparison.pair_recall == 1.0
    assert comparison.pair_precision < 1.0
    assert comparison.merged_proposals == 1


def test_only_assets_present_in_both_are_compared():
    proposals = [proposal(["a", "b", "unknown"])]
    reference = reference_grouping([["a", "b"], ["absent"]])

    comparison = GroupingReviewer().compare(proposals, reference)

    assert comparison.compared_assets == 2
    assert comparison.pair_f1 == 1.0


def test_photos_the_traveler_excluded_are_counted_when_grouped():
    proposals = [proposal(["a", "b", "leftout"])]
    reference = reference_grouping([["a", "b"]], excluded=["leftout"])

    comparison = GroupingReviewer().compare(proposals, reference)

    assert comparison.excluded_assets_grouped == 1
