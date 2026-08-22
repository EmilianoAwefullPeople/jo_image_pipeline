import json
import logging
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path

LOGGER = logging.getLogger(__name__)

SHA_PREFIX_LENGTH = 16
SUMMARY_STAMP_FORMAT = "%Y%m%dT%H%M%SZ"
SUMMARY_PREFIX = "run-"


@dataclass(frozen=True)
class ImageEvaluationRecord:
    dataset_id: str
    relative_path: str
    sha256: str
    model: str
    prompt_version: str
    schema_version: str
    derivative_version: str
    evaluated_utc: str
    attempts: int
    latency_ms: int
    prompt_tokens: int
    completion_tokens: int
    cost_usd: float
    generation_id: str | None
    validation_status: str
    evaluation: dict | None
    failure_detail: str | None


@dataclass(frozen=True)
class RunSummary:
    dataset_id: str
    model: str
    prompt_version: str
    schema_version: str
    started_utc: str
    finished_utc: str
    images_discovered: int
    images_skipped_existing: int
    images_evaluated: int
    valid: int
    invalid: int
    request_failed: int
    total_prompt_tokens: int
    total_completion_tokens: int
    total_cost_usd: float


def run_name(prompt_version: str, schema_version: str) -> str:
    return f"p{prompt_version}-{schema_version}"


class RunStore:
    def __init__(self, run_dir: Path):
        self.run_dir = run_dir

    @classmethod
    def for_versions(cls, llm_runs_dir: Path, dataset_id: str, prompt_version: str, schema_version: str) -> "RunStore":
        return cls(llm_runs_dir / dataset_id / run_name(prompt_version, schema_version))

    def record_path(self, sha256: str) -> Path:
        return self.run_dir / f"{sha256[:SHA_PREFIX_LENGTH]}.json"

    def exists(self, sha256: str) -> bool:
        return self.record_path(sha256).is_file()

    def records(self) -> list[dict]:
        if not self.run_dir.is_dir():
            LOGGER.info(f"no evaluation records stored yet at {self.run_dir}")
            return []

        payloads = []
        for path in sorted(self.run_dir.glob("*.json")):
            if path.name.startswith(SUMMARY_PREFIX):
                LOGGER.debug(f"{path.name}: skipped, run summary rather than an image record")
                continue
            payloads.append(json.loads(path.read_text()))

        LOGGER.info(f"loaded {len(payloads)} evaluation records from {self.run_dir}")
        return payloads

    def write_record(self, record: ImageEvaluationRecord) -> Path:
        target = self.record_path(record.sha256)
        if target.exists():
            raise FileExistsError(f"{record.relative_path}: evaluation record {target} already exists")

        self.run_dir.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(asdict(record), indent=2))
        LOGGER.info(f"{record.relative_path}: evaluation record written to {target}")
        return target

    def write_summary(self, summary: RunSummary) -> Path:
        stamp = datetime.fromisoformat(summary.finished_utc).strftime(SUMMARY_STAMP_FORMAT)
        target = self.run_dir / f"run-{stamp}.json"
        self.run_dir.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(asdict(summary), indent=2))
        LOGGER.info(f"{summary.dataset_id}: run summary written to {target}")
        return target
