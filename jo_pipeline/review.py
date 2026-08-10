import logging
from dataclasses import dataclass
from itertools import combinations

from jo_pipeline.group import GroupProposal
from jo_pipeline.reference import ReferenceGrouping

LOGGER = logging.getLogger(__name__)

REVIEW_VERSION = "review-1"


@dataclass(frozen=True)
class ReviewComparison:
    compared_assets: int
    reference_groups: int
    proposed_groups: int
    pair_precision: float
    pair_recall: float
    pair_f1: float
    split_groups: int
    merged_proposals: int
    excluded_assets_grouped: int
    method_version: str

    def as_dict(self) -> dict:
        return {
            "compared_assets": self.compared_assets,
            "reference_groups": self.reference_groups,
            "proposed_groups": self.proposed_groups,
            "pair_precision": self.pair_precision,
            "pair_recall": self.pair_recall,
            "pair_f1": self.pair_f1,
            "split_groups": self.split_groups,
            "merged_proposals": self.merged_proposals,
            "excluded_assets_grouped": self.excluded_assets_grouped,
            "method_version": self.method_version,
        }


class GroupingReviewer:
    def compare(self, proposals: list[GroupProposal], reference: ReferenceGrouping) -> ReviewComparison:
        proposal_of = self._proposal_index(proposals)
        comparable = reference.grouped_paths() & set(proposal_of)
        LOGGER.info(f"{reference.dataset_id}: comparing {len(comparable)} assets present in both the reference and the proposals")

        reference_pairs = self._pairs([[path for path in group.asset_paths if path in comparable] for group in reference.groups])
        proposed_pairs = self._pairs(self._proposed_members(proposal_of, comparable))
        agreed = reference_pairs & proposed_pairs

        precision = len(agreed) / len(proposed_pairs) if proposed_pairs else 0.0
        recall = len(agreed) / len(reference_pairs) if reference_pairs else 0.0
        harmonic = 2 * precision * recall / (precision + recall) if precision + recall else 0.0

        return ReviewComparison(
            compared_assets=len(comparable),
            reference_groups=len(reference.groups),
            proposed_groups=len(proposals),
            pair_precision=round(precision, 3),
            pair_recall=round(recall, 3),
            pair_f1=round(harmonic, 3),
            split_groups=self._split_groups(reference, proposal_of, comparable),
            merged_proposals=self._merged_proposals(reference, proposal_of, comparable),
            excluded_assets_grouped=len(set(reference.excluded_paths) & set(proposal_of)),
            method_version=REVIEW_VERSION,
        )

    def _proposal_index(self, proposals: list[GroupProposal]) -> dict:
        index = {}
        for position, proposal in enumerate(proposals):
            for member in proposal.members:
                index[member.relative_path] = position
        return index

    def _proposed_members(self, proposal_of: dict, comparable: set) -> list[list[str]]:
        grouped = {}
        for path in comparable:
            grouped.setdefault(proposal_of[path], []).append(path)
        return list(grouped.values())

    def _pairs(self, groups: list[list[str]]) -> set:
        pairs = set()
        for members in groups:
            pairs.update(frozenset(pair) for pair in combinations(sorted(members), 2))
        return pairs

    def _split_groups(self, reference: ReferenceGrouping, proposal_of: dict, comparable: set) -> int:
        splits = 0
        for group in reference.groups:
            landed = {proposal_of[path] for path in group.asset_paths if path in comparable}
            if len(landed) > 1:
                LOGGER.debug(f"reference group {group.index} was split across {len(landed)} proposals")
                splits += 1
        return splits

    def _merged_proposals(self, reference: ReferenceGrouping, proposal_of: dict, comparable: set) -> int:
        origins = {}
        for group in reference.groups:
            for path in group.asset_paths:
                if path in comparable:
                    origins.setdefault(proposal_of[path], set()).add(group.index)
        return sum(1 for groups in origins.values() if len(groups) > 1)
