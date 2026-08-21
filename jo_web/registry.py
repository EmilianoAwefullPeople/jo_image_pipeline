import logging
import shutil
import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

from jo_pipeline.group import GroupProposal
from jo_pipeline.reliability import FieldReliability
from jo_web.config import WebConfig
from llm_pipeline.prompts import PromptSet
from llm_pipeline.store import ImageEvaluationRecord, RunSummary

LOGGER = logging.getLogger(__name__)

RUN_ID_LENGTH = 32

CREATED = "created"
QUEUED = "queued"
INVENTORYING = "inventorying"
EXTRACTING = "extracting"
THUMBNAILING = "thumbnailing"
GROUPING = "grouping"
EVALUATING = "evaluating"
REFINING = "refining"
COMPLETE = "complete"
FAILED = "failed"

ACTIVE_STATUSES = (QUEUED, INVENTORYING, EXTRACTING, THUMBNAILING, GROUPING, EVALUATING, REFINING)


@dataclass(frozen=True)
class UploadedFile:
    filename: str
    size_bytes: int


@dataclass(frozen=True)
class SkippedFile:
    filename: str
    reason: str


@dataclass
class RunState:
    run_id: str
    created_utc: str
    status: str = CREATED
    stage: str = CREATED
    done: int = 0
    total: int = 0
    uploaded_bytes: int = 0
    files: list[UploadedFile] = field(default_factory=list)
    skipped: list[SkippedFile] = field(default_factory=list)
    reliability_rows: list[FieldReliability] = field(default_factory=list)
    reliability_failures: dict = field(default_factory=dict)
    images_analysed: int = 0
    groups: list[GroupProposal] = field(default_factory=list)
    baseline_groups: list[GroupProposal] = field(default_factory=list)
    thumbnails: dict = field(default_factory=dict)
    llm_summary: RunSummary | None = None
    llm_records: list[ImageEvaluationRecord] = field(default_factory=list)
    prompts: PromptSet | None = None
    failure_detail: str | None = None


class RunRegistry:
    def __init__(self, config: WebConfig):
        self.config = config
        self._runs = {}
        self._lock = threading.Lock()

    def create(self) -> RunState:
        run_id = uuid.uuid4().hex
        state = RunState(run_id=run_id, created_utc=datetime.now(timezone.utc).isoformat())
        self.config.upload_dir(run_id).mkdir(parents=True, exist_ok=True)
        with self._lock:
            self._runs[run_id] = state
        LOGGER.info(f"{run_id}: run created at {self.config.run_dir(run_id)}")
        return state

    def get(self, run_id: str) -> RunState | None:
        if len(run_id) != RUN_ID_LENGTH or not all(character in "0123456789abcdef" for character in run_id):
            LOGGER.warning(f"{run_id}: rejected, not a valid run id")
            return None

        with self._lock:
            state = self._runs.get(run_id)
        if state is None:
            LOGGER.info(f"{run_id}: no such run in the registry")
        return state

    def set_stage(self, run_id: str, stage: str, done: int, total: int) -> RunState | None:
        with self._lock:
            state = self._runs.get(run_id)
            if state is None:
                LOGGER.warning(f"{run_id}: stage {stage} discarded, run no longer registered")
                return None
            state.status = stage
            state.stage = stage
            state.done = done
            state.total = total
        LOGGER.info(f"{run_id}: stage {stage} at {done}/{total}")
        return state

    def set_extraction(self, run_id: str, rows: list[FieldReliability], failures: dict, images_analysed: int) -> RunState | None:
        with self._lock:
            state = self._runs.get(run_id)
            if state is None:
                return None
            state.reliability_rows = rows
            state.reliability_failures = failures
            state.images_analysed = images_analysed
        LOGGER.info(f"{run_id}: extraction recorded {images_analysed} images with {len(failures)} failures")
        return state

    def set_prompts(self, run_id: str, prompts: PromptSet) -> RunState | None:
        with self._lock:
            state = self._runs.get(run_id)
            if state is None:
                return None
            state.prompts = prompts
        LOGGER.info(f"{run_id}: prompt version {prompts.version} attached, {len(prompts.system)} system characters")
        return state

    def set_thumbnails(self, run_id: str, thumbnails: dict) -> RunState | None:
        with self._lock:
            state = self._runs.get(run_id)
            if state is None:
                return None
            state.thumbnails = thumbnails
        LOGGER.info(f"{run_id}: {len(thumbnails)} thumbnails available")
        return state

    def set_groups(self, run_id: str, groups: list[GroupProposal], baseline: list[GroupProposal]) -> RunState | None:
        with self._lock:
            state = self._runs.get(run_id)
            if state is None:
                return None
            state.groups = groups
            state.baseline_groups = baseline
        LOGGER.info(f"{run_id}: {len(groups)} group proposals recorded against a {len(baseline)} proposal baseline")
        return state

    def set_evaluation(self, run_id: str, summary: RunSummary, records: list[ImageEvaluationRecord]) -> RunState | None:
        with self._lock:
            state = self._runs.get(run_id)
            if state is None:
                return None
            state.llm_summary = summary
            state.llm_records = records
        LOGGER.info(f"{run_id}: evaluation recorded {len(records)} records at ${summary.total_cost_usd:.4f}")
        return state

    def set_failed(self, run_id: str, detail: str) -> RunState | None:
        with self._lock:
            state = self._runs.get(run_id)
            if state is None:
                LOGGER.warning(f"{run_id}: failure discarded, run no longer registered")
                return None
            state.status = FAILED
            state.stage = FAILED
            state.failure_detail = detail
        LOGGER.warning(f"{run_id}: run failed, {detail}")
        return state

    def add_file(self, run_id: str, uploaded: UploadedFile) -> RunState | None:
        with self._lock:
            state = self._runs.get(run_id)
            if state is None:
                return None
            state.files.append(uploaded)
            state.uploaded_bytes += uploaded.size_bytes
        LOGGER.info(f"{run_id}: accepted {uploaded.filename} at {uploaded.size_bytes} bytes")
        return state

    def add_skipped(self, run_id: str, skipped: SkippedFile) -> RunState | None:
        with self._lock:
            state = self._runs.get(run_id)
            if state is None:
                return None
            state.skipped.append(skipped)
        LOGGER.info(f"{run_id}: skipped {skipped.filename}, {skipped.reason}")
        return state

    def claim_for_queue(self, run_id: str) -> RunState | None:
        with self._lock:
            state = self._runs.get(run_id)
            if state is None or state.status != CREATED:
                LOGGER.info(f"{run_id}: not queued, status is {state.status if state else 'missing'}")
                return None
            state.status = QUEUED
            state.stage = QUEUED
        LOGGER.info(f"{run_id}: queued for processing")
        return state

    def existing_names(self, run_id: str) -> set:
        with self._lock:
            state = self._runs.get(run_id)
            if state is None:
                return set()
            return {uploaded.filename for uploaded in state.files}

    def snapshot(self) -> list[RunState]:
        with self._lock:
            return list(self._runs.values())

    def delete(self, run_id: str) -> bool:
        with self._lock:
            state = self._runs.pop(run_id, None)
        if state is None:
            LOGGER.info(f"{run_id}: delete skipped, run not registered")
            return False

        shutil.rmtree(self.config.run_dir(run_id), ignore_errors=True)
        LOGGER.info(f"{run_id}: run deleted with its directory")
        return True

    def bytes_on_disk(self) -> int:
        total = 0
        for state in self.snapshot():
            total += state.uploaded_bytes
        return total

    def expired(self) -> list[str]:
        cutoff = datetime.now(timezone.utc) - timedelta(minutes=self.config.run_ttl_minutes)
        stale = []
        for state in self.snapshot():
            if datetime.fromisoformat(state.created_utc) < cutoff:
                stale.append(state.run_id)
            else:
                LOGGER.debug(f"{state.run_id}: within the {self.config.run_ttl_minutes} minute retention window")
        return stale


def orphan_directories(runs_dir: Path, known: set) -> list[Path]:
    if not runs_dir.is_dir():
        return []
    return [path for path in runs_dir.iterdir() if path.is_dir() and path.name not in known]
