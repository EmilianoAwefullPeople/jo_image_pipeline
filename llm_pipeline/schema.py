from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

SCHEMA_VERSION = "llm-eval-2"
RESPONSE_SCHEMA_NAME = "image_evaluation"
BOUND_KEYWORDS = ("minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum", "minItems", "maxItems")

OTHER = "other"
DESCRIPTION_MIN_WORDS = 12
DESCRIPTION_MAX_WORDS = 17
MAX_NOTABLE_SUBJECTS = 4
MIN_KEYWORD_TAGS = 3
MAX_KEYWORD_TAGS = 6

SceneSettingType = Literal[
    "beach",
    "street",
    "restaurant",
    "museum_landmark",
    "transit",
    "accommodation",
    "market",
    "nightlife",
    "park",
    "coastline",
    "mountain_nature",
    "countryside",
    "other",
]
ConfidenceTier = Literal["high", "medium", "low"]
FocalPointType = Literal[
    "single_person",
    "group_of_people",
    "couple",
    "humans_in_background_only",
    "animals",
    "food_drink",
    "landmark_architecture",
    "natural_landscape",
    "object_item",
    "no_clear_subject",
    "other",
]
ActivityType = Literal[
    "eating_drinking",
    "walking_exploring",
    "swimming_beach",
    "hiking_outdoors",
    "viewing_sightseeing",
    "transit",
    "resting",
    "celebrating",
    "chatting_socializing",
    "shopping",
    "other",
]
EnvironmentType = Literal["city", "park", "coastline", "mountain_nature", "historic_district", "countryside", "other"]
CompositionType = Literal["wide_landscape", "close_up_macro", "portrait_framing", "panoramic", "action_motion", "aerial_elevated", "other"]
WeatherCondition = Literal["sunny_clear", "rainy", "snowy", "foggy_hazy", "overcast", "stormy", "unclear_indoor"]
PhotographicStyleType = Literal[
    "vibrant_saturated",
    "muted_desaturated",
    "black_and_white_monochrome",
    "warm_toned",
    "cool_toned",
    "high_contrast_dramatic",
    "soft_pastel",
    "shallow_depth_of_field",
    "motion_blur",
    "film_like_grainy",
    "minimalist_clean",
    "other",
]
TravelRelevance = Literal["travel_relevant", "not_travel_relevant", "not_applicable"]
KeepSignal = Literal["keep", "leave_out", "unsure"]


def reject_unexplained_other(field_name: str, types: list[str], other_detail: str | None):
    if OTHER in types and not other_detail:
        raise ValueError(f"{field_name} selects other, so other_detail must say what it was")


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SceneSetting(StrictModel):
    types: list[SceneSettingType] = Field(min_length=1, description="Every setting category that applies to this photo")
    other_detail: str | None = Field(description="Names the setting when types includes other, otherwise null")

    @model_validator(mode="after")
    def _explain_other(self) -> "SceneSetting":
        reject_unexplained_other("scene_setting", list(self.types), self.other_detail)
        return self


class LandmarkCandidate(StrictModel):
    name: str | None = Field(description="Landmark or point of interest name, null when none is identifiable")
    confidence_tier: ConfidenceTier | None = Field(description="high, medium or low, null when no name is given")
    evidence: str | None = Field(description="The visible features that support the name, null when no name is given")

    @model_validator(mode="after")
    def _name_requires_support(self) -> "LandmarkCandidate":
        if self.name is None:
            return self
        if self.confidence_tier is None or self.evidence is None:
            raise ValueError("a landmark candidate name requires a confidence tier and evidence")
        return self


class ActivitySelection(StrictModel):
    types: list[ActivityType] = Field(description="Every activity depicted, empty when the photo depicts none")
    other_detail: str | None = Field(description="Names the activity when types includes other, otherwise null")

    @model_validator(mode="after")
    def _explain_other(self) -> "ActivitySelection":
        reject_unexplained_other("activity", list(self.types), self.other_detail)
        return self


class EnvironmentStyle(StrictModel):
    types: list[EnvironmentType] = Field(min_length=1, description="General environment categories that apply")
    other_detail: str | None = Field(description="Names the environment when types includes other, otherwise null")
    specific_style: str | None = Field(description="A short phrase for the visual or cultural character, null when nothing distinct enough")

    @model_validator(mode="after")
    def _explain_other(self) -> "EnvironmentStyle":
        reject_unexplained_other("environment", list(self.types), self.other_detail)
        return self


class PhotographicStyle(StrictModel):
    types: list[PhotographicStyleType] = Field(min_length=1, description="Every stylistic treatment that applies")
    other_detail: str | None = Field(description="Names the treatment when types includes other, otherwise null")

    @model_validator(mode="after")
    def _explain_other(self) -> "PhotographicStyle":
        reject_unexplained_other("photographic_style", list(self.types), self.other_detail)
        return self


class ScreenshotJudgment(StrictModel):
    is_screenshot_or_document: bool
    travel_relevance: TravelRelevance
    document_kind: str | None
    confidence: float = Field(ge=0.0, le=1.0)


class MemoryAssessment(StrictModel):
    keep_signal: KeepSignal
    reason: str
    confidence: float = Field(ge=0.0, le=1.0)


class RepresentativeQuality(StrictModel):
    score: float = Field(ge=0.0, le=1.0)
    reasoning: str


class ImageEvaluation(StrictModel):
    general_description: str = Field(description=f"A holistic description of the photo in {DESCRIPTION_MIN_WORDS} to {DESCRIPTION_MAX_WORDS} words")
    scene_setting: SceneSetting
    landmark: LandmarkCandidate
    notable_subjects: list[str] = Field(max_length=MAX_NOTABLE_SUBJECTS, description="Predominant non-human subjects as 'Category, specific name', empty when none")
    focal_points: list[FocalPointType] = Field(min_length=1, description="Every focal point type the composition carries")
    activity: ActivitySelection
    environment: EnvironmentStyle
    composition: list[CompositionType] = Field(min_length=1, description="Every framing type that applies")
    weather: list[WeatherCondition] = Field(min_length=1, description="Every visible weather condition, unclear_indoor when the sky is not readable")
    keyword_tags: list[str] = Field(min_length=MIN_KEYWORD_TAGS, max_length=MAX_KEYWORD_TAGS, description=f"{MIN_KEYWORD_TAGS} to {MAX_KEYWORD_TAGS} short tags or phrases")
    photographic_style: PhotographicStyle
    screenshot: ScreenshotJudgment
    memory: MemoryAssessment
    representative_quality: RepresentativeQuality

    @model_validator(mode="after")
    def _description_stays_in_band(self) -> "ImageEvaluation":
        words = len(self.general_description.split())
        if not DESCRIPTION_MIN_WORDS <= words <= DESCRIPTION_MAX_WORDS:
            raise ValueError(f"general_description must be {DESCRIPTION_MIN_WORDS} to {DESCRIPTION_MAX_WORDS} words, it was {words}")
        return self


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
