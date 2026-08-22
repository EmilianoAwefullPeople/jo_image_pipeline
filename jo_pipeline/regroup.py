import logging
from dataclasses import dataclass, replace

from jo_pipeline.assets import AssetSignals
from jo_pipeline.evaluation_readers import ImageSignal
from jo_pipeline.group import BURST, DUPLICATE, MEMBER, REPRESENTATIVE, GroupProposal, MomentSequence, ProposalBuilder, haversine_metres, time_gap_seconds
from jo_pipeline.refine import LEAVE_OUT, ProposalRefiner
from llm_pipeline.review import ReviewFrame, ReviewSession, build_session
from llm_pipeline.schema import MomentReview

LOGGER = logging.getLogger(__name__)

REGROUP_VERSION = "regroup-1"
SESSION_GAP_SECONDS = 8 * 60 * 60
SESSION_MAX_FRAMES = 40
MIN_SESSION_FRAMES = 2
PRIMARY_MEMBERSHIPS = (MEMBER, REPRESENTATIVE)
ATTACHED_MEMBERSHIPS = (DUPLICATE, BURST)
NO_DESCRIPTION = "no description available"
MOMENT_REVIEW_SOURCE = "moment_review"
REVIEW_SPLIT = "review_split"
NOT_APPLICABLE = "not_applicable"

REVIEWED = "reviewed"
FALLBACK = "fallback"
SKIPPED = "skipped"

UNCHANGED = "unchanged"
MERGED = "merged"
SPLIT = "split"
RESEGMENTED = "resegmented"


@dataclass(frozen=True)
class ProposalFrames:
    proposal: GroupProposal
    assets: list[AssetSignals]


class SessionBuilder:
    def build(self, dataset_id: str, source_version: str, proposals: list[GroupProposal], signals: dict, image_signals: dict) -> list[ReviewSession]:
        if not image_signals:
            LOGGER.info(f"{dataset_id}: no per-image descriptions, nothing to review")
            return []

        anchored = [self._frames(proposal, signals) for proposal in proposals if proposal.start_utc is not None]
        anchored = [frames for frames in anchored if frames.assets]
        chunks = []
        for run in self._split_runs(anchored):
            chunks.extend(self._cap(run))

        sessions = []
        for chunk in chunks:
            count = sum(len(frames.assets) for frames in chunk)
            if count < MIN_SESSION_FRAMES:
                LOGGER.info(f"{dataset_id}: {chunk[0].proposal.label} holds {count} photo, nothing to review in it")
                continue
            sessions.append(build_session(dataset_id, source_version, self._review_frames(chunk, image_signals)))
        LOGGER.info(f"{dataset_id}: {len(sessions)} review sessions built from {len(anchored)} proposals")
        return sessions

    def _frames(self, proposal: GroupProposal, signals: dict) -> ProposalFrames:
        assets = [signals[member.relative_path] for member in proposal.members if member.membership in PRIMARY_MEMBERSHIPS and member.relative_path in signals]
        return ProposalFrames(proposal=proposal, assets=sorted(assets, key=lambda asset: asset.captured_utc))

    def _split_runs(self, anchored: list[ProposalFrames]) -> list[list[ProposalFrames]]:
        runs = []
        for frames in anchored:
            if runs and time_gap_seconds(runs[-1][-1].assets[-1].captured_utc, frames.assets[0].captured_utc) <= SESSION_GAP_SECONDS:
                runs[-1].append(frames)
            else:
                if runs:
                    LOGGER.debug(f"{frames.proposal.label}: opens a new review session after a gap beyond {SESSION_GAP_SECONDS}s")
                runs.append([frames])
        return runs

    def _cap(self, run: list[ProposalFrames]) -> list[list[ProposalFrames]]:
        count = sum(len(frames.assets) for frames in run)
        if count <= SESSION_MAX_FRAMES or len(run) == 1:
            return [run]

        gaps = [time_gap_seconds(previous.assets[-1].captured_utc, current.assets[0].captured_utc) for previous, current in zip(run, run[1:])]
        cut = gaps.index(max(gaps)) + 1
        LOGGER.info(f"{run[0].proposal.label}: session of {count} photos cut at its largest gap into {cut} and {len(run) - cut} proposals")
        return self._cap(run[:cut]) + self._cap(run[cut:])

    def _review_frames(self, chunk: list[ProposalFrames], image_signals: dict) -> list[ReviewFrame]:
        frames = []
        previous = None
        for frames_of in chunk:
            for position, asset in enumerate(frames_of.assets):
                signal = image_signals.get(asset.relative_path)
                boundary = frames_of.proposal.evidence.get("opened_by") if position == 0 and previous is not None else None
                frames.append(ReviewFrame(
                    index=len(frames),
                    relative_path=asset.relative_path,
                    sha256=asset.sha256,
                    captured_utc=asset.captured_utc.isoformat(),
                    gap_seconds=None if previous is None else round(time_gap_seconds(previous.captured_utc, asset.captured_utc), 1),
                    distance_metres=None if previous is None else self._distance(previous, asset),
                    boundary_before=boundary,
                    summary=signal.summary if signal else NO_DESCRIPTION,
                ))
                previous = asset
        return frames

    def _distance(self, previous: AssetSignals, current: AssetSignals) -> float | None:
        distance = haversine_metres(previous, current)
        return None if distance is None else round(distance, 1)


class RegroupApplier:
    def __init__(self):
        self.builder = ProposalBuilder()

    def apply(self, proposals: list[GroupProposal], signals: dict, sessions: list[ReviewSession], reviews: dict) -> list[GroupProposal]:
        session_of = {frame.relative_path: session for session in sessions for frame in session.frames}
        links = {member.relative_path: member for proposal in proposals for member in proposal.members if member.membership in ATTACHED_MEMBERSHIPS}
        consumed = set()
        output = []
        for proposal in proposals:
            if proposal.start_utc is None:
                output.append(self._passthrough(proposal, SKIPPED, "no capture timestamps, nothing to review"))
                continue

            session = session_of.get(first_primary(proposal))
            if session is None:
                output.append(self._passthrough(proposal, SKIPPED, "not part of any review session"))
                continue
            if session.session_id in consumed:
                continue

            consumed.add(session.session_id)
            members = [candidate for candidate in proposals if first_primary(candidate) in {frame.relative_path for frame in session.frames}]
            review = reviews.get(session.session_id)
            if review is None:
                LOGGER.info(f"{session.session_id}: no valid review, {len(members)} baseline proposals pass through")
                output.extend(self._passthrough(member, FALLBACK, f"session {session.session_id} has no valid review") for member in members)
            else:
                output.extend(self._moments(session, review, members, signals, links))
        LOGGER.info(f"regrouping produced {len(output)} proposals from {len(proposals)} baseline proposals across {len(consumed)} sessions")
        return output

    def _passthrough(self, proposal: GroupProposal, status: str, detail: str) -> GroupProposal:
        evidence = dict(proposal.evidence)
        evidence["review"] = {"status": status, "detail": detail}
        return replace(proposal, evidence=evidence)

    def _moments(self, session: ReviewSession, review: MomentReview, members: list[GroupProposal], signals: dict, links: dict) -> list[GroupProposal]:
        frames = session.frames
        opens = {first_primary(proposal): proposal for proposal in members}
        closes = {last_primary(proposal): proposal for proposal in members}
        owner = {member.relative_path: proposal for proposal in members for member in proposal.members}

        opened_by = []
        for position, moment in enumerate(review.moments):
            first_path = frames[moment.first_frame].relative_path
            if position == 0 or first_path in opens:
                opened_by.append(opens[first_path].evidence.get("opened_by"))
            else:
                opened_by.append({"kind": REVIEW_SPLIT, "after": frames[moment.first_frame - 1].relative_path, "reason": moment.reason})

        proposals = []
        for position, moment in enumerate(review.moments):
            paths = [frame.relative_path for frame in frames[moment.first_frame:moment.last_frame + 1]]
            assets = [signals[path] for path in paths]
            closed_by = opened_by[position + 1] if position + 1 < len(review.moments) else closes[paths[-1]].evidence.get("closed_by")
            proposal = self.builder.build(MomentSequence(assets=assets, opened_by=opened_by[position], closed_by=closed_by), links)

            contributors = []
            for path in paths:
                if owner[path] not in contributors:
                    contributors.append(owner[path])
            evidence = dict(proposal.evidence)
            evidence["review"] = {
                "session_id": session.session_id,
                "status": REVIEWED,
                "title": moment.title,
                "reason": moment.reason,
                "frames": [moment.first_frame, moment.last_frame],
                "baseline_labels": [contributor.label for contributor in contributors],
                "change": self._change(paths, contributors),
            }
            evidence["bridged_boundaries"] = [opens[path].evidence.get("opened_by") for path in paths[1:] if path in opens]
            LOGGER.info(f"{proposal.label}: {evidence['review']['change']} by the moment review as '{moment.title}' from {len(contributors)} baseline proposal(s)")
            proposals.append(replace(proposal, evidence=evidence))
        return proposals

    def _change(self, paths: list[str], contributors: list[GroupProposal]) -> str:
        chosen = set(paths)
        wholly_inside = [set(primary_paths(contributor)) <= chosen for contributor in contributors]
        if len(contributors) == 1 and wholly_inside[0]:
            return UNCHANGED
        if all(wholly_inside):
            return MERGED
        if len(contributors) == 1:
            return SPLIT
        return RESEGMENTED


class Regrouper:
    def __init__(self):
        self.applier = RegroupApplier()
        self.refiner = ProposalRefiner()

    def regroup(self, proposals: list[GroupProposal], signals: dict, image_signals: dict, sessions: list[ReviewSession], reviews: dict) -> list[GroupProposal]:
        regrouped = self.applier.apply(proposals, signals, sessions, reviews)
        refined = self.refiner.refine(regrouped, self.review_signals(image_signals, sessions, reviews))
        return [replace(proposal, method_version=REGROUP_VERSION) for proposal in refined]

    def review_signals(self, image_signals: dict, sessions: list[ReviewSession], reviews: dict) -> dict:
        signals = dict(image_signals)
        for session in sessions:
            review = reviews.get(session.session_id)
            if review is None:
                continue
            for suggestion in review.leave_out:
                path = session.frames[suggestion.frame].relative_path
                existing = signals.get(path)
                if existing and existing.keep_signal == LEAVE_OUT:
                    LOGGER.info(f"{path}: the review agrees with the per-image leave_out, per-image verdict kept")
                    continue
                LOGGER.info(f"{path}: the review suggests leave_out at {suggestion.confidence}, {suggestion.reason}")
                signals[path] = self._leave_out(path, existing, suggestion.reason, suggestion.confidence)
        return signals

    def _leave_out(self, path: str, existing: ImageSignal | None, reason: str, confidence: float) -> ImageSignal:
        if existing:
            return replace(existing, keep_signal=LEAVE_OUT, keep_reason=reason, keep_confidence=confidence, keep_source=MOMENT_REVIEW_SOURCE)
        return ImageSignal(
            relative_path=path,
            keep_signal=LEAVE_OUT,
            keep_reason=reason,
            keep_confidence=confidence,
            keep_source=MOMENT_REVIEW_SOURCE,
            representative_score=0.0,
            representative_reasoning="",
            is_screenshot=False,
            travel_relevance=NOT_APPLICABLE,
            scene_setting_types=[],
            summary=NO_DESCRIPTION,
        )


def first_primary(proposal: GroupProposal) -> str | None:
    return next((member.relative_path for member in proposal.members if member.membership in PRIMARY_MEMBERSHIPS), None)


def last_primary(proposal: GroupProposal) -> str | None:
    return next((member.relative_path for member in reversed(proposal.members) if member.membership in PRIMARY_MEMBERSHIPS), None)


def primary_paths(proposal: GroupProposal) -> list[str]:
    return [member.relative_path for member in proposal.members if member.membership in PRIMARY_MEMBERSHIPS]
