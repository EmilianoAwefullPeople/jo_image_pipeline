import logging
from dataclasses import dataclass, replace

from jo_pipeline.evaluation_readers import reader_for
from jo_pipeline.group import COHESION_WEIGHT, LOCATION_WEIGHT, MEMBER, QUALITY_WEIGHT, REPRESENTATIVE, GroupMember, GroupProposal
from jo_pipeline.normalize import MetadataObservation

LOGGER = logging.getLogger(__name__)

REFINER_VERSION = "refine-1"
FETCHED_CATEGORY = "fetched"
NULL_REASON = "model returned null"

KEEP = "keep"
LEAVE_OUT = "leave_out"
UNSURE = "unsure"
NOT_TRAVEL_RELEVANT = "not_travel_relevant"


@dataclass(frozen=True)
class MemberPartition:
    kept: list
    excluded: list


def build_image_signals(schema_version: str, evaluations: dict) -> dict:
    reader = reader_for(schema_version)
    signals = {}
    for relative_path, evaluation in evaluations.items():
        if not evaluation:
            LOGGER.debug(f"{relative_path}: no evaluation available, no signal built")
            continue

        signal = reader.signal(relative_path, evaluation)
        signals[relative_path] = signal
        LOGGER.debug(f"{relative_path}: signal built with keep {signal.keep_signal} and quality {signal.representative_score}")
    LOGGER.info(f"built {len(signals)} image signals from {len(evaluations)} {schema_version} evaluations")
    return signals


def build_llm_observations(evaluation: dict, model_id: str, method_version: str) -> list[MetadataObservation]:
    source = f"llm.{model_id}"
    scene_setting = evaluation["scene_setting"]
    landmark = evaluation["landmark"]
    activity = evaluation["activity"]
    environment = evaluation["environment"]
    style = evaluation["photographic_style"]
    screenshot = evaluation["screenshot"]
    memory = evaluation["memory"]
    quality = evaluation["representative_quality"]

    fields = [
        ("general_description", evaluation["general_description"], None, {}),
        ("scene_setting_types", scene_setting["types"] or None, None, {"other_detail": scene_setting["other_detail"]}),
        ("landmark_candidate", landmark["name"], None, {"confidence_tier": landmark["confidence_tier"], "evidence": landmark["evidence"]}),
        ("notable_subjects", evaluation["notable_subjects"] or None, None, {}),
        ("focal_point_types", evaluation["focal_points"] or None, None, {}),
        ("activity_types", activity["types"] or None, None, {"other_detail": activity["other_detail"]}),
        ("environment_general_types", environment["types"] or None, None, {"other_detail": environment["other_detail"]}),
        ("environment_specific_style", environment["specific_style"], None, {}),
        ("composition_types", evaluation["composition"] or None, None, {}),
        ("weather_conditions", evaluation["weather"] or None, None, {}),
        ("keyword_tags", evaluation["keyword_tags"] or None, None, {}),
        ("photographic_style_types", style["types"] or None, None, {"other_detail": style["other_detail"]}),
        ("why_tags", evaluation["why_tags"] or None, None, {}),
        ("is_screenshot_or_document", screenshot["is_screenshot_or_document"], screenshot["confidence"], {"travel_relevance": screenshot["travel_relevance"], "document_kind": screenshot["document_kind"]}),
        ("memory_keep_signal", memory["keep_signal"], memory["confidence"], {"reason": memory["reason"]}),
        ("representative_quality", quality["score"], None, {"reasoning": quality["reasoning"]}),
    ]

    observations = []
    for field_name, value, confidence, evidence in fields:
        observations.append(MetadataObservation(
            field=field_name,
            category=FETCHED_CATEGORY,
            value=value,
            raw_value=None,
            source=source,
            method_version=method_version,
            confidence=confidence,
            evidence=evidence,
            unknown_reason=None if value is not None else NULL_REASON,
        ))
    LOGGER.debug(f"flattened an evaluation into {len(observations)} fetched observations from {source}")
    return observations


class ProposalRefiner:
    def refine(self, proposals: list[GroupProposal], signals: dict) -> list[GroupProposal]:
        if not signals:
            LOGGER.info(f"no image signals supplied, {len(proposals)} proposals pass through unrefined")
            return list(proposals)

        refined = []
        for proposal in proposals:
            candidate = self._refine_proposal(proposal, signals)
            if candidate:
                refined.append(candidate)
            else:
                LOGGER.info(f"{proposal.label}: proposal dropped, every member was marked leave_out")

        LOGGER.info(f"refinement produced {len(refined)} proposals from {len(proposals)} baseline proposals")
        return refined

    def _refine_proposal(self, proposal: GroupProposal, signals: dict) -> GroupProposal | None:
        partition = self._partition(proposal, signals)
        if not partition.kept:
            return None

        members = self._elect_representative(partition.kept, signals)
        evidence = dict(proposal.evidence)
        evidence["baseline_score"] = proposal.score
        evidence["baseline_method_version"] = proposal.method_version
        evidence["member_count"] = len(members)
        if partition.excluded:
            evidence["excluded_by_signal"] = partition.excluded
        else:
            LOGGER.debug(f"{proposal.label}: no member was marked leave_out")

        flagged = self._flagged_screenshots(members, signals)
        if flagged:
            evidence["screenshots_flagged"] = flagged
            LOGGER.info(f"{proposal.label}: {len(flagged)} member(s) flagged as non travel relevant screenshots, retained for review")

        score = self._refined_score(proposal, members, signals, evidence)
        return replace(proposal, members=members, score=score, method_version=REFINER_VERSION, evidence=evidence)

    def _partition(self, proposal: GroupProposal, signals: dict) -> MemberPartition:
        kept = []
        excluded = []
        for member in proposal.members:
            signal = signals.get(member.relative_path)
            if signal and signal.keep_signal == LEAVE_OUT:
                excluded.append({
                    "relative_path": member.relative_path,
                    "membership": member.membership,
                    "reason": signal.keep_reason,
                    "confidence": signal.keep_confidence,
                    "source": signal.keep_source,
                })
                LOGGER.info(f"{member.relative_path}: dropped from {proposal.label}, {signal.keep_source} marked it leave_out at {signal.keep_confidence}")
            else:
                kept.append(member)
        return MemberPartition(kept=kept, excluded=excluded)

    def _elect_representative(self, members: list[GroupMember], signals: dict) -> list[GroupMember]:
        scored = {member.relative_path: self._quality(member, signals) for member in members}
        best = max(scored, key=scored.get)
        previous = next((member.relative_path for member in members if member.membership == REPRESENTATIVE), None)
        if previous == best:
            LOGGER.debug(f"{best}: remains the representative under model quality scoring")
        else:
            LOGGER.info(f"{best}: elected representative at quality {scored[best]:.2f}, replacing {previous}")

        elected = []
        for member in members:
            if member.relative_path == best:
                evidence = dict(member.evidence)
                evidence.update({
                    "reason": "highest model representative quality",
                    "representative_score": scored[best],
                    "previous_representative": previous,
                })
                signal = signals.get(best)
                if signal:
                    evidence["reasoning"] = signal.representative_reasoning
                elected.append(GroupMember(member.relative_path, REPRESENTATIVE, evidence))
            elif member.membership == REPRESENTATIVE:
                elected.append(GroupMember(member.relative_path, MEMBER, {"reason": "superseded as representative by model quality"}))
            else:
                elected.append(member)
        return elected

    def _quality(self, member: GroupMember, signals: dict) -> float:
        signal = signals.get(member.relative_path)
        if signal:
            return signal.representative_score

        LOGGER.debug(f"{member.relative_path}: no model quality available, scored zero for representative election")
        return 0.0

    def _flagged_screenshots(self, members: list[GroupMember], signals: dict) -> list[dict]:
        flagged = []
        for member in members:
            signal = signals.get(member.relative_path)
            if signal and signal.is_screenshot and signal.travel_relevance == NOT_TRAVEL_RELEVANT:
                flagged.append({"relative_path": member.relative_path, "scene_setting_types": signal.scene_setting_types})
        return flagged

    def _refined_score(self, proposal: GroupProposal, members: list[GroupMember], signals: dict, evidence: dict) -> float:
        components = dict(proposal.evidence.get("score_components", {}))
        scored = [signals[member.relative_path].representative_score for member in members if member.relative_path in signals]
        if not scored:
            LOGGER.debug(f"{proposal.label}: no model quality for any member, baseline score retained")
            return proposal.score

        components["model_quality"] = round(sum(scored) / len(scored), 3)
        cohesion = components.get("cohesion", 0.0)
        location = components.get("location_support", 0.0)
        score = COHESION_WEIGHT * cohesion + LOCATION_WEIGHT * location + QUALITY_WEIGHT * components["model_quality"]
        evidence["score_components"] = components
        LOGGER.debug(f"{proposal.label}: refined score {score:.3f} from cohesion {cohesion}, location {location}, model quality {components['model_quality']}")
        return round(score, 3)
