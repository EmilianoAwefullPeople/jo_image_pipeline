import logging
from dataclasses import dataclass

LOGGER = logging.getLogger(__name__)

SCHEMA_V1 = "llm-eval-1"
SCHEMA_V2 = "llm-eval-2"
SUMMARY_SEPARATOR = " | "
IMAGE_EVALUATION_SOURCE = "image_evaluation"


@dataclass(frozen=True)
class ImageSignal:
    relative_path: str
    keep_signal: str
    keep_reason: str
    keep_confidence: float
    keep_source: str
    representative_score: float
    representative_reasoning: str
    is_screenshot: bool
    travel_relevance: str
    scene_setting_types: list
    summary: str


class EvaluationReader:
    def signal(self, relative_path: str, evaluation: dict) -> ImageSignal:
        raise NotImplementedError


class V1Reader(EvaluationReader):
    def signal(self, relative_path: str, evaluation: dict) -> ImageSignal:
        memory = evaluation["memory"]
        quality = evaluation["representative_quality"]
        screenshot = evaluation["screenshot"]
        return ImageSignal(
            relative_path=relative_path,
            keep_signal=memory["keep_signal"],
            keep_reason=memory["reason"],
            keep_confidence=memory["confidence"],
            keep_source=IMAGE_EVALUATION_SOURCE,
            representative_score=quality["score"],
            representative_reasoning=quality["reasoning"],
            is_screenshot=screenshot["is_screenshot_or_document"],
            travel_relevance=screenshot["travel_relevance"],
            scene_setting_types=[evaluation["scene"]["scene_type"]],
            summary=self._summary(evaluation),
        )

    def _summary(self, evaluation: dict) -> str:
        landmark = evaluation["landmark"]
        parts = [
            evaluation["caption"],
            f"scene: {evaluation['scene']['scene_type']}",
            f"activity: {evaluation['activity']['description']}" if evaluation["activity"]["description"] else None,
            f"landmark: {landmark['name']} (confidence {landmark['confidence']})" if landmark["name"] else None,
            f"keep: {evaluation['memory']['keep_signal']}",
            f"quality: {evaluation['representative_quality']['score']}",
        ]
        return join_summary(parts)


class V2Reader(EvaluationReader):
    def signal(self, relative_path: str, evaluation: dict) -> ImageSignal:
        memory = evaluation["memory"]
        quality = evaluation["representative_quality"]
        screenshot = evaluation["screenshot"]
        return ImageSignal(
            relative_path=relative_path,
            keep_signal=memory["keep_signal"],
            keep_reason=memory["reason"],
            keep_confidence=memory["confidence"],
            keep_source=IMAGE_EVALUATION_SOURCE,
            representative_score=quality["score"],
            representative_reasoning=quality["reasoning"],
            is_screenshot=screenshot["is_screenshot_or_document"],
            travel_relevance=screenshot["travel_relevance"],
            scene_setting_types=evaluation["scene_setting"]["types"],
            summary=self._summary(evaluation),
        )

    def _summary(self, evaluation: dict) -> str:
        landmark = evaluation["landmark"]
        environment = evaluation["environment"]
        activity = evaluation["activity"]["types"]
        parts = [
            evaluation["general_description"],
            f"setting: {', '.join(evaluation['scene_setting']['types'])}",
            f"activity: {', '.join(activity)}" if activity else None,
            f"landmark: {landmark['name']} ({landmark['confidence_tier']})" if landmark["name"] else None,
            f"environment: {environment['specific_style']}" if environment["specific_style"] else None,
            f"tags: {', '.join(evaluation['keyword_tags'])}" if evaluation["keyword_tags"] else None,
            f"keep: {evaluation['memory']['keep_signal']}",
            f"quality: {evaluation['representative_quality']['score']}",
        ]
        return join_summary(parts)


READERS = {SCHEMA_V1: V1Reader(), SCHEMA_V2: V2Reader()}


def reader_for(schema_version: str) -> EvaluationReader:
    reader = READERS.get(schema_version)
    if reader is None:
        raise ValueError(f"no evaluation reader for schema version {schema_version}, known versions are {sorted(READERS)}")
    LOGGER.debug(f"reading evaluations with the {schema_version} reader")
    return reader


def join_summary(parts: list) -> str:
    return SUMMARY_SEPARATOR.join(part for part in parts if part)
