import logging
import statistics
from dataclasses import dataclass, field
from datetime import datetime
from math import asin, cos, radians, sin, sqrt

from jo_pipeline.assets import AssetSignals
from jo_pipeline.extract import hamming_distance

LOGGER = logging.getLogger(__name__)

GROUPER_VERSION = "group-1"

MIN_WINDOW_SECONDS = 15 * 60
MAX_WINDOW_SECONDS = 45 * 60
WINDOW_MULTIPLIER = 8
CADENCE_SAMPLE = 5
PLACE_THRESHOLD_METRES = 150.0
EARTH_RADIUS_METRES = 6371000.0

BURST_MAX_SECONDS = 3.0
NEAR_DUPLICATE_BITS = 6
BLUR_REFERENCE = 500.0

COHESION_WEIGHT = 0.4
LOCATION_WEIGHT = 0.3
QUALITY_WEIGHT = 0.3

MEMBER = "member"
REPRESENTATIVE = "representative"
DUPLICATE = "duplicate"
BURST = "burst"


@dataclass(frozen=True)
class GroupMember:
    relative_path: str
    membership: str
    evidence: dict = field(default_factory=dict)


@dataclass(frozen=True)
class GroupProposal:
    label: str
    start_utc: str | None
    end_utc: str | None
    score: float
    method_version: str
    members: list[GroupMember]
    evidence: dict = field(default_factory=dict)


class DuplicateIndex:
    def classify(self, signals: list[AssetSignals]) -> dict:
        links = {}
        seen_hashes = {}
        seen_content = {}
        for asset in signals:
            canonical = seen_content.get(asset.sha256)
            if canonical:
                links[asset.relative_path] = GroupMember(asset.relative_path, DUPLICATE, {"canonical": canonical, "reason": "identical content hash"})
                LOGGER.info(f"{asset.relative_path}: exact duplicate of {canonical}")
                continue

            seen_content[asset.sha256] = asset.relative_path
            match = self._nearest_hash(asset, seen_hashes)
            if match:
                links[asset.relative_path] = match
            else:
                seen_hashes[asset.relative_path] = asset
                LOGGER.debug(f"{asset.relative_path}: recorded as a distinct visual asset")
        return links

    def _nearest_hash(self, asset: AssetSignals, seen_hashes: dict) -> GroupMember | None:
        if not asset.difference_hash:
            return None

        distances = {
            canonical_path: hamming_distance(asset.difference_hash, candidate.difference_hash)
            for canonical_path, candidate in seen_hashes.items()
            if candidate.difference_hash
        }
        within_threshold = {path: bits for path, bits in distances.items() if bits <= NEAR_DUPLICATE_BITS}
        if not within_threshold:
            return None

        canonical_path = min(within_threshold, key=within_threshold.get)
        distance = within_threshold[canonical_path]
        seconds = time_gap_seconds(seen_hashes[canonical_path].captured_utc, asset.captured_utc)
        if seconds is not None and seconds <= BURST_MAX_SECONDS:
            LOGGER.info(f"{asset.relative_path}: burst frame of {canonical_path} at {seconds:.1f}s and {distance} bits")
            return GroupMember(asset.relative_path, BURST, {"canonical": canonical_path, "hash_distance": distance, "gap_seconds": seconds})

        LOGGER.info(f"{asset.relative_path}: near duplicate of {canonical_path} at {distance} bits")
        return GroupMember(asset.relative_path, DUPLICATE, {"canonical": canonical_path, "hash_distance": distance, "reason": "difference hash within threshold"})


class MomentGrouper:
    def __init__(self):
        self.duplicates = DuplicateIndex()

    def group(self, signals: list[AssetSignals]) -> list[GroupProposal]:
        links = self.duplicates.classify(signals)
        anchored = sorted([asset for asset in signals if asset.captured_utc and asset.relative_path not in links], key=lambda asset: asset.captured_utc)
        unanchored = [asset for asset in signals if not asset.captured_utc and asset.relative_path not in links]

        proposals = [self._build_proposal(sequence, links) for sequence in self._split(anchored)]
        if unanchored:
            proposals.append(self._unanchored_proposal(unanchored))

        LOGGER.info(f"grouping produced {len(proposals)} proposals from {len(signals)} assets with {len(links)} duplicate or burst links")
        return proposals

    def _split(self, anchored: list[AssetSignals]) -> list[list[AssetSignals]]:
        if not anchored:
            return []

        sequences = [[anchored[0]]]
        gaps = []
        for previous, current in zip(anchored, anchored[1:]):
            gap = time_gap_seconds(previous.captured_utc, current.captured_utc)
            reason = self._boundary_reason(previous, current, gap, gaps)
            if reason:
                LOGGER.debug(f"{current.relative_path}: boundary after {previous.relative_path} because {reason}")
                sequences.append([current])
                gaps = []
            else:
                sequences[-1].append(current)
                gaps.append(gap)
        return sequences

    def _boundary_reason(self, previous: AssetSignals, current: AssetSignals, gap: float, gaps: list[float]) -> str | None:
        window = self._adaptive_window(gaps)
        if gap > window:
            return f"time gap {gap:.0f}s exceeded window {window:.0f}s"

        distance = haversine_metres(previous, current)
        if distance is not None and distance > PLACE_THRESHOLD_METRES:
            return f"moved {distance:.0f}m beyond the {PLACE_THRESHOLD_METRES:.0f}m place threshold"
        return None

    def _adaptive_window(self, gaps: list[float]) -> float:
        if not gaps:
            return MAX_WINDOW_SECONDS

        cadence = statistics.median(gaps[-CADENCE_SAMPLE:])
        return min(max(cadence * WINDOW_MULTIPLIER, MIN_WINDOW_SECONDS), MAX_WINDOW_SECONDS)

    def _build_proposal(self, sequence: list[AssetSignals], links: dict) -> GroupProposal:
        members = [GroupMember(asset.relative_path, MEMBER) for asset in sequence]
        members.extend(self._attached_links(sequence, links))
        members = self._elect_representative(members, sequence)

        located = [asset for asset in sequence if asset.latitude is not None]
        span_seconds = time_gap_seconds(sequence[0].captured_utc, sequence[-1].captured_utc)
        evidence = {
            "member_count": len(members),
            "primary_count": len(sequence),
            "attached_count": len(members) - len(sequence),
            "span_seconds": span_seconds,
            "located_members": len(located),
            "unlocated_members": len(sequence) - len(located),
            "max_distance_metres": self._max_distance(located),
        }
        components = self._score_components(sequence, located, span_seconds)
        evidence["score_components"] = components

        return GroupProposal(
            label=self._label(sequence),
            start_utc=sequence[0].captured_utc.isoformat(),
            end_utc=sequence[-1].captured_utc.isoformat(),
            score=self._weighted_score(components),
            method_version=GROUPER_VERSION,
            members=members,
            evidence=evidence,
        )

    def _attached_links(self, sequence: list[AssetSignals], links: dict) -> list[GroupMember]:
        paths = {asset.relative_path for asset in sequence}
        return [link for link in links.values() if link.evidence.get("canonical") in paths]

    def _elect_representative(self, members: list[GroupMember], sequence: list[AssetSignals]) -> list[GroupMember]:
        sharpness = {asset.relative_path: asset.blur_score or 0.0 for asset in sequence}
        best = max(sharpness, key=sharpness.get)
        elected = []
        for member in members:
            if member.relative_path == best:
                elected.append(GroupMember(member.relative_path, REPRESENTATIVE, {"reason": "sharpest member", "blur_score": sharpness[best]}))
            else:
                elected.append(member)
        return elected

    def _max_distance(self, located: list[AssetSignals]) -> float | None:
        if len(located) < 2:
            return None

        distances = [haversine_metres(located[0], asset) for asset in located[1:]]
        return round(max(distances), 1)

    def _score_components(self, sequence: list[AssetSignals], located: list[AssetSignals], span_seconds: float) -> dict:
        cohesion = 1.0 - min(span_seconds / (len(sequence) * MAX_WINDOW_SECONDS), 1.0)
        quality = statistics.mean([min((asset.blur_score or 0.0) / BLUR_REFERENCE, 1.0) for asset in sequence])
        return {
            "cohesion": round(cohesion, 3),
            "location_support": round(len(located) / len(sequence), 3),
            "visual_quality": round(quality, 3),
        }

    def _weighted_score(self, components: dict) -> float:
        score = (
            COHESION_WEIGHT * components["cohesion"]
            + LOCATION_WEIGHT * components["location_support"]
            + QUALITY_WEIGHT * components["visual_quality"]
        )
        return round(score, 3)

    def _label(self, sequence: list[AssetSignals]) -> str:
        start = sequence[0].captured_utc
        end = sequence[-1].captured_utc
        return f"{start.strftime('%Y-%m-%d %H:%M')} to {end.strftime('%H:%M')} UTC"

    def _unanchored_proposal(self, unanchored: list[AssetSignals]) -> GroupProposal:
        LOGGER.info(f"{len(unanchored)} assets have no capture timestamp and cannot be placed on the timeline")
        return GroupProposal(
            label="Unanchored assets",
            start_utc=None,
            end_utc=None,
            score=0.0,
            method_version=GROUPER_VERSION,
            members=[GroupMember(asset.relative_path, MEMBER, {"reason": "no capture timestamp"}) for asset in unanchored],
            evidence={"member_count": len(unanchored), "reason": "no capture timestamp and no coordinate to place these assets"},
        )


def time_gap_seconds(earlier: datetime | None, later: datetime | None) -> float | None:
    if earlier is None or later is None:
        return None
    return abs((later - earlier).total_seconds())


def haversine_metres(start: AssetSignals, end: AssetSignals) -> float | None:
    if start.latitude is None or end.latitude is None:
        return None

    start_latitude, end_latitude = radians(start.latitude), radians(end.latitude)
    delta_latitude = end_latitude - start_latitude
    delta_longitude = radians(end.longitude - start.longitude)
    factor = sin(delta_latitude / 2) ** 2 + cos(start_latitude) * cos(end_latitude) * sin(delta_longitude / 2) ** 2
    return 2 * EARTH_RADIUS_METRES * asin(sqrt(factor))
