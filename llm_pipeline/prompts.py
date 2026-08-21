from dataclasses import dataclass
from pathlib import Path

PROMPT_VERSION = "2"
CUSTOM_PROMPT_VERSION = "custom"
PROMPTS_DIR = Path(__file__).resolve().parent / "prompts"
UNKNOWN_CAPTURE_TIME = "unknown"
CAPTURE_TIME_PLACEHOLDER = "{capture_local_time}"


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


def load_system_prompt() -> str:
    return (PROMPTS_DIR / f"system_v{PROMPT_VERSION}.txt").read_text()


def load_user_template() -> str:
    return (PROMPTS_DIR / f"user_v{PROMPT_VERSION}.txt").read_text()


def default_prompts() -> PromptSet:
    return PromptSet(version=PROMPT_VERSION, system=load_system_prompt(), user_template=load_user_template())


def custom_prompts(system: str, user_template: str) -> PromptSet:
    return PromptSet(version=CUSTOM_PROMPT_VERSION, system=system, user_template=user_template)


def template_reject_reason(user_template: str) -> str | None:
    try:
        user_template.format(capture_local_time=UNKNOWN_CAPTURE_TIME)
    except (IndexError, KeyError, ValueError) as error:
        return f"the user template is not a usable format string, {type(error).__name__}: {error}"
    return None
