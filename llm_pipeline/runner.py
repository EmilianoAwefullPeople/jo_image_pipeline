import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from PIL import Image, UnidentifiedImageError

from llm_pipeline.client import OpenRouterClient, OpenRouterError
from llm_pipeline.derivative import DERIVATIVE_VERSION, DerivativeBuilder
from llm_pipeline.discovery import DiscoveredImage
from llm_pipeline.evaluate import VALID, EvaluationOutcome, image_evaluator
from llm_pipeline.prompts import PromptSet
from llm_pipeline.schema import SCHEMA_VERSION
from llm_pipeline.store import ImageEvaluationRecord, RunStore, RunSummary

LOGGER = logging.getLogger(__name__)

DEFAULT_CONCURRENCY = 4
RETRY_STATUS_CODES = (429, 500, 502, 503, 504)
MAX_REQUEST_ATTEMPTS = 3
BACKOFF_BASE_SECONDS = 2.0


@dataclass(frozen=True)
class EvaluationProgress:
    done: int
    total: int
    relative_path: str
    status: str


@dataclass(frozen=True)
class ImageAttempt:
    record: ImageEvaluationRecord | None
    failure_detail: str | None


@dataclass(frozen=True)
class EvaluationRun:
    summary: RunSummary
    records: list[ImageEvaluationRecord]


def pending_images(store: RunStore, images: list[DiscoveredImage]) -> list[DiscoveredImage]:
    pending = []
    for image in images:
        if store.exists(image.sha256):
            LOGGER.info(f"{image.relative_path}: already evaluated, skipped")
        else:
            LOGGER.debug(f"{image.relative_path}: pending evaluation")
            pending.append(image)
    return pending


def group_by_hash(images: list[DiscoveredImage]) -> dict[str, list[DiscoveredImage]]:
    grouped = {}
    for image in images:
        grouped.setdefault(image.sha256, []).append(image)
    for sha256, members in grouped.items():
        if len(members) > 1:
            shared = ", ".join(image.relative_path for image in members[1:])
            LOGGER.info(f"{members[0].relative_path}: byte identical to {shared}, evaluating once for hash {sha256[:12]}")
        else:
            LOGGER.debug(f"{members[0].relative_path}: unique content hash {sha256[:12]}")
    return grouped


def call_with_retry(label: str, attempt_call: Callable[[], EvaluationOutcome]) -> EvaluationOutcome:
    attempt = 0
    while True:
        attempt += 1
        try:
            return attempt_call()
        except OpenRouterError as error:
            if error.status_code not in RETRY_STATUS_CODES or attempt >= MAX_REQUEST_ATTEMPTS:
                LOGGER.warning(f"{label}: status {error.status_code} on attempt {attempt}, giving up")
                raise
            delay = BACKOFF_BASE_SECONDS * (2 ** (attempt - 1))
            LOGGER.info(f"{label}: status {error.status_code} on attempt {attempt}, retrying in {delay:.0f}s")
            time.sleep(delay)


def build_record(dataset_id: str, image: DiscoveredImage, model: str, prompt_version: str, outcome: EvaluationOutcome) -> ImageEvaluationRecord:
    evaluation = None if outcome.evaluation is None else outcome.evaluation.model_dump()
    return ImageEvaluationRecord(
        dataset_id=dataset_id,
        relative_path=image.relative_path,
        sha256=image.sha256,
        model=model,
        prompt_version=prompt_version,
        schema_version=SCHEMA_VERSION,
        derivative_version=DERIVATIVE_VERSION,
        evaluated_utc=datetime.now(timezone.utc).isoformat(),
        attempts=outcome.attempts,
        latency_ms=outcome.latency_ms,
        prompt_tokens=outcome.prompt_tokens,
        completion_tokens=outcome.completion_tokens,
        cost_usd=outcome.cost_usd,
        generation_id=outcome.generation_id,
        validation_status=outcome.validation_status,
        evaluation=evaluation,
        failure_detail=outcome.failure_detail,
    )


class EvaluationRunner:
    def __init__(self, client: OpenRouterClient, store: RunStore, dataset_id: str, dataset_path: Path, prompts: PromptSet, concurrency: int = DEFAULT_CONCURRENCY):
        self.client = client
        self.store = store
        self.dataset_id = dataset_id
        self.dataset_path = dataset_path
        self.prompts = prompts
        self.concurrency = concurrency
        self.evaluator = image_evaluator(client)
        self.builder = DerivativeBuilder(dataset_path)
        self._lock = threading.Lock()
        self._done = 0

    def run(self, images: list[DiscoveredImage], limit: int | None = None, on_progress: Callable[[EvaluationProgress], None] | None = None) -> EvaluationRun:
        started_utc = datetime.now(timezone.utc).isoformat()
        pending = pending_images(self.store, images)
        skipped_existing = len(images) - len(pending)
        if limit is not None:
            LOGGER.info(f"{self.dataset_id}: limit {limit} applied to {len(pending)} pending images")
            pending = pending[:limit]
        grouped = group_by_hash(pending)
        total = len(grouped)
        LOGGER.info(f"{self.dataset_id}: evaluating {total} unique images from {len(pending)} pending, {skipped_existing} already stored")

        self._done = 0
        attempts = self._evaluate_all([members[0] for members in grouped.values()], total, on_progress)
        records = sorted([attempt.record for attempt in attempts if attempt.record], key=lambda record: record.relative_path)
        request_failed = sum(1 for attempt in attempts if attempt.record is None)

        summary = RunSummary(
            dataset_id=self.dataset_id,
            model=self.client.model,
            prompt_version=self.prompts.version,
            schema_version=SCHEMA_VERSION,
            started_utc=started_utc,
            finished_utc=datetime.now(timezone.utc).isoformat(),
            images_discovered=len(images),
            images_skipped_existing=skipped_existing,
            images_evaluated=len(records),
            valid=sum(1 for record in records if record.validation_status == VALID),
            invalid=sum(1 for record in records if record.validation_status != VALID),
            request_failed=request_failed,
            total_prompt_tokens=sum(record.prompt_tokens for record in records),
            total_completion_tokens=sum(record.completion_tokens for record in records),
            total_cost_usd=sum(record.cost_usd for record in records),
        )
        self.store.write_summary(summary)
        return EvaluationRun(summary=summary, records=records)

    def _evaluate_all(self, images: list[DiscoveredImage], total: int, on_progress: Callable[[EvaluationProgress], None] | None) -> list[ImageAttempt]:
        if not images:
            LOGGER.info(f"{self.dataset_id}: nothing pending, no requests sent")
            return []

        attempts = []
        with ThreadPoolExecutor(max_workers=self.concurrency) as pool:
            futures = {pool.submit(self._evaluate_one, image): image for image in images}
            for future in as_completed(futures):
                image = futures[future]
                attempt = future.result()
                attempts.append(attempt)
                self._report(image, attempt, total, on_progress)
        return attempts

    def _report(self, image: DiscoveredImage, attempt: ImageAttempt, total: int, on_progress: Callable[[EvaluationProgress], None] | None):
        with self._lock:
            self._done += 1
            done = self._done
        status = attempt.record.validation_status if attempt.record else "failed"
        if on_progress:
            on_progress(EvaluationProgress(done=done, total=total, relative_path=image.relative_path, status=status))
        else:
            LOGGER.debug(f"{image.relative_path}: no progress listener attached")

    def _evaluate_one(self, image: DiscoveredImage) -> ImageAttempt:
        try:
            derivative = self.builder.build(image.relative_path)
        except (OSError, UnidentifiedImageError, ValueError, Image.DecompressionBombError) as error:
            detail = f"derivative failed with {type(error).__name__}: {error}"
            LOGGER.warning(f"{image.relative_path}: {detail}")
            return ImageAttempt(record=None, failure_detail=detail)

        messages = self.prompts.build_messages(derivative.data_url, derivative.capture_local_time)
        try:
            outcome = call_with_retry(image.relative_path, lambda: self.evaluator.evaluate(image.relative_path, messages))
        except OpenRouterError as error:
            detail = f"request failed with status {error.status_code}: {error}"
            LOGGER.warning(f"{image.relative_path}: {detail}")
            return ImageAttempt(record=None, failure_detail=detail)

        record = build_record(self.dataset_id, image, self.client.model, self.prompts.version, outcome)
        self.store.write_record(record)
        LOGGER.info(f"{image.relative_path}: {record.validation_status} attempts={record.attempts} cost=${record.cost_usd:.4f}")
        return ImageAttempt(record=record, failure_detail=None)
