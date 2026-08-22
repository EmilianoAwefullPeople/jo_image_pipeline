from dataclasses import dataclass
from pathlib import Path

from llm_pipeline.styles import GroupingStyle

PROMPT_VERSION = "3"
REVIEW_PROMPT_VERSION = "1"
TOPIC_PROMPT_VERSION = "1"
CUSTOM_PROMPT_VERSION = "custom"
PROMPTS_DIR = Path(__file__).resolve().parent / "prompts"
UNKNOWN_CAPTURE_TIME = "unknown"
CAPTURE_TIME_PLACEHOLDER = "{capture_local_time}"
SESSION_PLACEHOLDER = "{session}"


@dataclass(frozen=True)
class PromptSet:
    version: str
    system: str
    user_template: str

    def build_messages(self, data_url: str, capture_local_time: str | None) -> list[dict]:
        user_text = self.user_template.format(capture_local_time=capture_local_time or UNKNOWN_CAPTURE_TIME).strip()
        return [
            {"role": "system", "content": self.system},
            {
                "role": "user",
                "content": [
                    {"type": "image_url", "image_url": {"url": data_url}},
                    {"type": "text", "text": user_text},
                ],
            },
        ]


@dataclass(frozen=True)
class ReviewPrompts:
    version: str
    system: str
    user_template: str

    def build_messages(self, session_text: str) -> list[dict]:
        return [
            {"role": "system", "content": self.system},
            {"role": "user", "content": self.user_template.format(session=session_text).strip()},
        ]


def load_system_prompt() -> str:
    return (PROMPTS_DIR / f"system_v{PROMPT_VERSION}.txt").read_text()


def load_user_template() -> str:
    return (PROMPTS_DIR / f"user_v{PROMPT_VERSION}.txt").read_text()


def default_prompts() -> PromptSet:
    return PromptSet(version=PROMPT_VERSION, system=load_system_prompt(), user_template=load_user_template())


def default_review_prompts() -> ReviewPrompts:
    return ReviewPrompts(
        version=REVIEW_PROMPT_VERSION,
        system=(PROMPTS_DIR / f"review_system_v{REVIEW_PROMPT_VERSION}.txt").read_text(),
        user_template=(PROMPTS_DIR / f"review_user_v{REVIEW_PROMPT_VERSION}.txt").read_text(),
    )


@dataclass(frozen=True)
class TopicPrompts:
    version: str
    style: GroupingStyle
    system: str
    user_template: str

    def build_messages(self, session_text: str) -> list[dict]:
        return [
            {"role": "system", "content": self.system},
            {"role": "user", "content": self.user_template.format(style_name=self.style.name, session=session_text).strip()},
        ]


def default_topic_prompts(style: GroupingStyle) -> TopicPrompts:
    system = (PROMPTS_DIR / f"topic_system_v{TOPIC_PROMPT_VERSION}.txt").read_text().format(style_instructions=style.instructions)
    return TopicPrompts(
        version=f"topic-{style.id}-{TOPIC_PROMPT_VERSION}",
        style=style,
        system=system,
        user_template=(PROMPTS_DIR / f"topic_user_v{TOPIC_PROMPT_VERSION}.txt").read_text(),
    )


def custom_prompts(system: str, user_template: str) -> PromptSet:
    return PromptSet(version=CUSTOM_PROMPT_VERSION, system=system, user_template=user_template)


def template_reject_reason(user_template: str) -> str | None:
    try:
        user_template.format(capture_local_time=UNKNOWN_CAPTURE_TIME)
    except (IndexError, KeyError, ValueError) as error:
        return f"the user template is not a usable format string, {type(error).__name__}: {error}"
    return None
