import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

from jo_pipeline.config import REPO_ROOT, PipelineConfig, resolve_path
from llm_pipeline.config import LlmConfig

DEFAULT_RUNS_DIR = "data/web_runs"
DEFAULT_MAX_FILES = 60
DEFAULT_MAX_FILE_MEGABYTES = 40
DEFAULT_MAX_RUN_MEGABYTES = 600
DEFAULT_MAX_MEGAPIXELS = 60
DEFAULT_RUN_TTL_MINUTES = 120
DEFAULT_DISK_BUDGET_MEGABYTES = 3000
DEFAULT_LLM_CONCURRENCY = 4
DEFAULT_HOST = "0.0.0.0"
DEFAULT_PORT = 8080
DEFAULT_MODEL = "openai/gpt-5.6-sol"

UPLOAD_DATASET_ID = "input"
UPLOAD_DIR_NAME = "input"
THUMBNAIL_DIR_NAME = "thumbnails"
MEGABYTE = 1024 * 1024

ACCEPTED_EXTENSIONS = {".heic", ".heif", ".jpg", ".jpeg", ".png", ".tif", ".tiff"}


@dataclass(frozen=True)
class WebConfig:
    runs_dir: Path
    max_files: int
    max_file_bytes: int
    max_run_bytes: int
    max_pixels: int
    run_ttl_minutes: int
    disk_budget_bytes: int
    llm_concurrency: int
    openrouter_api_key: str
    openrouter_model: str
    log_level: str
    host: str
    port: int

    def run_dir(self, run_id: str) -> Path:
        return self.runs_dir / run_id

    def upload_dir(self, run_id: str) -> Path:
        return self.run_dir(run_id) / UPLOAD_DIR_NAME

    def thumbnail_dir(self, run_id: str) -> Path:
        return self.run_dir(run_id) / THUMBNAIL_DIR_NAME

    def pipeline_config(self, run_id: str) -> PipelineConfig:
        run_dir = self.run_dir(run_id)
        return PipelineConfig(
            dataset_root=run_dir,
            data_dir=run_dir,
            database_path=run_dir / "unused.sqlite3",
            openrouter_api_key=self.openrouter_api_key,
            openrouter_model=self.openrouter_model,
            log_level=self.log_level,
        )

    def llm_config(self, run_id: str) -> LlmConfig:
        run_dir = self.run_dir(run_id)
        return LlmConfig(
            dataset_root=run_dir,
            data_dir=run_dir,
            openrouter_api_key=self.openrouter_api_key,
            openrouter_model=self.openrouter_model,
            log_level=self.log_level,
        )


def read_int(name: str, default: int) -> int:
    return int(os.environ.get(name, default))


def load_web_config() -> WebConfig:
    load_dotenv(REPO_ROOT / ".env")
    return WebConfig(
        runs_dir=resolve_path(os.environ.get("JO_WEB_RUNS_DIR", DEFAULT_RUNS_DIR)),
        max_files=read_int("JO_WEB_MAX_FILES", DEFAULT_MAX_FILES),
        max_file_bytes=read_int("JO_WEB_MAX_FILE_MEGABYTES", DEFAULT_MAX_FILE_MEGABYTES) * MEGABYTE,
        max_run_bytes=read_int("JO_WEB_MAX_RUN_MEGABYTES", DEFAULT_MAX_RUN_MEGABYTES) * MEGABYTE,
        max_pixels=read_int("JO_WEB_MAX_MEGAPIXELS", DEFAULT_MAX_MEGAPIXELS) * 1_000_000,
        run_ttl_minutes=read_int("JO_WEB_RUN_TTL_MINUTES", DEFAULT_RUN_TTL_MINUTES),
        disk_budget_bytes=read_int("JO_WEB_DISK_BUDGET_MEGABYTES", DEFAULT_DISK_BUDGET_MEGABYTES) * MEGABYTE,
        llm_concurrency=read_int("JO_WEB_LLM_CONCURRENCY", DEFAULT_LLM_CONCURRENCY),
        openrouter_api_key=os.environ.get("JO_OPENROUTER_API_KEY", ""),
        openrouter_model=os.environ.get("JO_OPENROUTER_MODEL", DEFAULT_MODEL),
        log_level=os.environ.get("JO_LOG_LEVEL", "INFO"),
        host=os.environ.get("JO_WEB_HOST", DEFAULT_HOST),
        port=read_int("JO_WEB_PORT", DEFAULT_PORT),
    )
