import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DATASET_ROOT = "resources/images"
DEFAULT_DATA_DIR = "data"
DEFAULT_DATABASE_PATH = "data/jo_pipeline.sqlite3"
DEFAULT_MODEL = "openai/gpt-5.6-sol"

SCHEMA_VERSION = "1"
PROMPT_VERSION = "1"


@dataclass(frozen=True)
class PipelineConfig:
    dataset_root: Path
    data_dir: Path
    database_path: Path
    openrouter_api_key: str
    openrouter_model: str
    log_level: str

    @property
    def manifest_dir(self) -> Path:
        return self.data_dir / "manifests"

    @property
    def runs_dir(self) -> Path:
        return self.data_dir / "runs"

    def dataset_path(self, dataset_id: str) -> Path:
        return self.dataset_root / dataset_id


def resolve_path(value: str) -> Path:
    path = Path(value).expanduser()
    if path.is_absolute():
        return path
    return REPO_ROOT / path


def load_config() -> PipelineConfig:
    load_dotenv(REPO_ROOT / ".env")
    return PipelineConfig(
        dataset_root=resolve_path(os.environ.get("JO_DATASET_ROOT", DEFAULT_DATASET_ROOT)),
        data_dir=resolve_path(os.environ.get("JO_DATA_DIR", DEFAULT_DATA_DIR)),
        database_path=resolve_path(os.environ.get("JO_DATABASE_PATH", DEFAULT_DATABASE_PATH)),
        openrouter_api_key=os.environ.get("JO_OPENROUTER_API_KEY", ""),
        openrouter_model=os.environ.get("JO_OPENROUTER_MODEL", DEFAULT_MODEL),
        log_level=os.environ.get("JO_LOG_LEVEL", "DEBUG"),
    )
