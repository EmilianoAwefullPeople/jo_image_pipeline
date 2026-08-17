from pathlib import Path

PROMPT_VERSION = "1"
PROMPTS_DIR = Path(__file__).resolve().parent / "prompts"
UNKNOWN_CAPTURE_TIME = "unknown"


def load_system_prompt() -> str:
    return (PROMPTS_DIR / f"system_v{PROMPT_VERSION}.txt").read_text()


def load_user_template() -> str:
    return (PROMPTS_DIR / f"user_v{PROMPT_VERSION}.txt").read_text()


def build_messages(data_url: str, capture_local_time: str | None) -> list[dict]:
    user_text = load_user_template().format(capture_local_time=capture_local_time or UNKNOWN_CAPTURE_TIME).strip()
    return [
        {"role": "system", "content": load_system_prompt()},
        {
            "role": "user",
            "content": [
                {"type": "image_url", "image_url": {"url": data_url}},
                {"type": "text", "text": user_text},
            ],
        },
    ]
