from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

SCHEMA_VERSION = "llm-eval-1"
RESPONSE_SCHEMA_NAME = "image_evaluation"
BOUND_KEYWORDS = ("minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum")

SceneType = Literal[
    "landmark",
    "landscape_nature",
    "cityscape_street",
    "food_drink",
    "accommodation",
    "transport",
    "people_moment",
    "activity_event",
    "shop_market",
    "document_or_screen",
    "other",
]
EmotionName = Literal["awe", "excitement", "meaningful", "calm", "fun", "connection"]
TextKind = Literal["sign", "menu", "ticket", "label", "screen", "handwriting", "other"]
TravelRelevance = Literal["travel_relevant", "not_travel_relevant", "not_applicable"]
KeepSignal = Literal["keep", "leave_out", "unsure"]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SceneAssessment(StrictModel):
    scene_type: SceneType
    confidence: float = Field(ge=0.0, le=1.0)
    evidence: str


class ActivityAssessment(StrictModel):
    description: str | None
    confidence: float = Field(ge=0.0, le=1.0)
    evidence: str | None


class LandmarkCandidate(StrictModel):
    name: str | None
    confidence: float = Field(ge=0.0, le=1.0)
    evidence: str | None

    @model_validator(mode="after")
    def _name_requires_evidence(self) -> "LandmarkCandidate":
        if self.name is not None and self.evidence is None:
            raise ValueError("a landmark candidate name requires evidence")
        return self


class VisibleText(StrictModel):
    transcription: str | None
    text_kind: TextKind | None
    language: str | None
    confidence: float = Field(ge=0.0, le=1.0)


class ScreenshotJudgment(StrictModel):
    is_screenshot_or_document: bool
    travel_relevance: TravelRelevance
    document_kind: str | None
    confidence: float = Field(ge=0.0, le=1.0)


class EmotionLabel(StrictModel):
    label: EmotionName
    confidence: float = Field(ge=0.0, le=1.0)


class MemoryAssessment(StrictModel):
    keep_signal: KeepSignal
    reason: str
    confidence: float = Field(ge=0.0, le=1.0)


class RepresentativeQuality(StrictModel):
    score: float = Field(ge=0.0, le=1.0)
    reasoning: str


class ImageEvaluation(StrictModel):
    caption: str
    scene: SceneAssessment
    activity: ActivityAssessment
    landmark: LandmarkCandidate
    visible_text: VisibleText
    screenshot: ScreenshotJudgment
    emotions: list[EmotionLabel]
    memory: MemoryAssessment
    representative_quality: RepresentativeQuality
    journaling_prompt: str | None


def response_schema() -> dict:
    schema = ImageEvaluation.model_json_schema()
    _apply_strict_rules(schema)
    return schema


def _apply_strict_rules(node: object):
    if isinstance(node, dict):
        if node.get("type") == "object":
            node["additionalProperties"] = False
        for keyword in BOUND_KEYWORDS:
            node.pop(keyword, None)
        for value in node.values():
            _apply_strict_rules(value)
    if isinstance(node, list):
        for item in node:
            _apply_strict_rules(item)
