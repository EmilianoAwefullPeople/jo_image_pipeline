import hashlib
import json
import logging
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from pydantic import BaseModel

from llm_pipeline.client import OpenRouterClient, OpenRouterError
from llm_pipeline.evaluate import VALID, EvaluationOutcome, StructuredEvaluator
from llm_pipeline.prompts import ReviewPrompts, TopicPrompts, default_review_prompts, default_topic_prompts
from llm_pipeline.runner import call_with_retry
from llm_pipeline.schema import REVIEW_SCHEMA_NAME, REVIEW_SCHEMA_VERSION, TOPIC_SCHEMA_NAME, TOPIC_SCHEMA_VERSION, MomentReview, TopicReview, review_schema, topic_schema
from llm_pipeline.store import SUMMARY_PREFIX, SUMMARY_STAMP_FORMAT
from llm_pipeline.styles import MOMENT_REVIEW, GroupingStyle

LOGGER = logging.getLogger(__name__)

SESSION_ID_LENGTH = 16
DEFAULT_REVIEW_CONCURRENCY = 2
TIME_GAP = "time_gap"
PLACE_CHANGE = "place_change"


@dataclass(frozen=True)
class ReviewFrame:
    index: int
    relative_path: str
    sha256: str
    captured_utc: str
    gap_seconds: float | None
    distance_metres: float | None
    boundary_before: dict | None
    summary: str


@dataclass(frozen=True)
class ReviewSession:
    session_id: str
    dataset_id: str
    source_version: str
    frames: list[ReviewFrame]


@dataclass(frozen=True)
class ReviewRecord:
    dataset_id: str
    session_id: str
    style: str
    frame_paths: list[str]
    model: str
    review_prompt_version: str
    review_schema_version: str
    source_version: str
    reviewed_utc: str
    attempts: int
    latency_ms: int
    prompt_tokens: int
    completion_tokens: int
    cost_usd: float
    generation_id: str | None
    validation_status: str
    review: dict | None
    failure_detail: str | None


@dataclass(frozen=True)
class ReviewSummary:
    dataset_id: str
    style: str
    model: str
    review_prompt_version: str
    review_schema_version: str
    source_version: str
    started_utc: str
    finished_utc: str
    sessions_total: int
    sessions_skipped_existing: int
    sessions_reviewed: int
    valid: int
    invalid: int
    request_failed: int
    total_prompt_tokens: int
    total_completion_tokens: int
    total_cost_usd: float


@dataclass(frozen=True)
class ReviewProgress:
    done: int
    total: int
    session_id: str
    status: str


@dataclass(frozen=True)
class SessionAttempt:
    record: ReviewRecord | None
    failure_detail: str | None


@dataclass(frozen=True)
class ReviewRun:
    summary: ReviewSummary
    records: list[ReviewRecord]


def build_session(dataset_id: str, source_version: str, frames: list[ReviewFrame], review_version: str, schema_version: str) -> ReviewSession:
    digest = hashlib.sha256()
    digest.update(source_version.encode())
    digest.update(review_version.encode())
    digest.update(schema_version.encode())
    for frame in frames:
        digest.update(json.dumps([frame.sha256, frame.captured_utc, frame.gap_seconds, frame.distance_metres, frame.boundary_before, frame.summary], sort_keys=True).encode())
    session_id = digest.hexdigest()[:SESSION_ID_LENGTH]
    LOGGER.debug(f"{session_id}: session of {len(frames)} frames built from {source_version} descriptions for {review_version}")
    return ReviewSession(session_id=session_id, dataset_id=dataset_id, source_version=source_version, frames=frames)


def render_session(session: ReviewSession) -> str:
    proposed = 1 + sum(1 for frame in session.frames if frame.boundary_before)
    lines = [f"Session of {len(session.frames)} photos in capture order, proposed as {proposed} moments. Times are UTC."]
    current_day = None
    for frame in session.frames:
        captured = datetime.fromisoformat(frame.captured_utc)
        day = captured.strftime("%Y-%m-%d")
        if day != current_day:
            lines.append(f"Day {day}")
            current_day = day
        if frame.boundary_before:
            lines.append(f"--- proposed boundary: {boundary_text(frame.boundary_before)} ---")
        lines.append(f"[{frame.index}] {captured.strftime('%H:%M')}{step_text(frame)} {frame.summary}")
    return "\n".join(lines)


def boundary_text(boundary: dict) -> str:
    if boundary["kind"] == TIME_GAP:
        return f"{duration_text(boundary['gap_seconds'])} gap, window {duration_text(boundary['window_seconds'])}"
    if boundary["kind"] == PLACE_CHANGE:
        return f"moved {metres_text(boundary['distance_metres'])}, threshold {metres_text(boundary['threshold_metres'])}"
    return boundary["kind"]


def step_text(frame: ReviewFrame) -> str:
    if frame.gap_seconds is None:
        return ""
    distance = f", {metres_text(frame.distance_metres)}" if frame.distance_metres is not None else ""
    return f" (+{duration_text(frame.gap_seconds)}{distance})"


def duration_text(seconds: float) -> str:
    if seconds < 90:
        return f"{round(seconds)} s"
    minutes = round(seconds / 60)
    if minutes < 90:
        return f"{minutes} min"
    hours, remainder = divmod(minutes, 60)
    return f"{hours} h {remainder} min"


def metres_text(value: float) -> str:
    if value < 1000:
        return f"{round(value)} m"
    return f"{value / 1000:.1f} km"


def review_parser(frame_count: int) -> Callable[[str], MomentReview]:
    def parse(content: str) -> MomentReview:
        review = MomentReview.model_validate(json.loads(content))
        review.covers(frame_count)
        return review
    return parse


def topic_parser(frame_count: int) -> Callable[[str], TopicReview]:
    def parse(content: str) -> TopicReview:
        review = TopicReview.model_validate(json.loads(content))
        review.within(frame_count)
        return review
    return parse


def prompts_for(style: GroupingStyle) -> ReviewPrompts | TopicPrompts:
    if style.kind == MOMENT_REVIEW:
        return default_review_prompts()
    return default_topic_prompts(style)


def schema_version_for(style: GroupingStyle) -> str:
    if style.kind == MOMENT_REVIEW:
        return REVIEW_SCHEMA_VERSION
    return TOPIC_SCHEMA_VERSION


def review_model_for(style: GroupingStyle) -> type[BaseModel]:
    if style.kind == MOMENT_REVIEW:
        return MomentReview
    return TopicReview


def review_run_name(review_version: str, schema_version: str) -> str:
    return f"review-r{review_version}-{schema_version}"


class ReviewStore:
    def __init__(self, llm_runs_dir: Path, dataset_id: str, review_version: str, schema_version: str):
        self.run_dir = llm_runs_dir / dataset_id / review_run_name(review_version, schema_version)

    @classmethod
    def for_style(cls, llm_runs_dir: Path, dataset_id: str, style: GroupingStyle) -> "ReviewStore":
        return cls(llm_runs_dir, dataset_id, prompts_for(style).version, schema_version_for(style))

    def record_path(self, session_id: str) -> Path:
        return self.run_dir / f"{session_id}.json"

    def exists(self, session_id: str) -> bool:
        return self.record_path(session_id).is_file()

    def records(self) -> list[dict]:
        if not self.run_dir.is_dir():
            LOGGER.info(f"no review records stored yet at {self.run_dir}")
            return []

        payloads = []
        for path in sorted(self.run_dir.glob("*.json")):
            if path.name.startswith(SUMMARY_PREFIX):
                LOGGER.debug(f"{path.name}: skipped, run summary rather than a review record")
                continue
            payloads.append(json.loads(path.read_text()))
        LOGGER.info(f"loaded {len(payloads)} review records from {self.run_dir}")
        return payloads

    def write_record(self, record: ReviewRecord) -> Path:
        target = self.record_path(record.session_id)
        if target.exists():
            raise FileExistsError(f"{record.session_id}: review record {target} already exists")

        self.run_dir.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(asdict(record), indent=2))
        LOGGER.info(f"{record.session_id}: review record written to {target}")
        return target

    def write_summary(self, summary: ReviewSummary) -> Path:
        stamp = datetime.fromisoformat(summary.finished_utc).strftime(SUMMARY_STAMP_FORMAT)
        target = self.run_dir / f"{SUMMARY_PREFIX}{stamp}.json"
        self.run_dir.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(asdict(summary), indent=2))
        LOGGER.info(f"{summary.dataset_id}: review summary written to {target}")
        return target


def pending_sessions(store: ReviewStore, sessions: list[ReviewSession]) -> list[ReviewSession]:
    pending = []
    for session in sessions:
        if store.exists(session.session_id):
            LOGGER.info(f"{session.session_id}: already reviewed, skipped")
        else:
            LOGGER.debug(f"{session.session_id}: pending review")
            pending.append(session)
    return pending


def review_records_for(store: ReviewStore, sessions: list[ReviewSession]) -> list[dict]:
    wanted = {session.session_id for session in sessions}
    records = []
    for record in store.records():
        if record["session_id"] in wanted:
            records.append(record)
        else:
            LOGGER.debug(f"{record['session_id']}: review record belongs to a session that no longer exists, ignored")
    return records


def parse_reviews(records: list[dict], model_class: type[BaseModel]) -> dict:
    reviews = {}
    for record in records:
        if record["validation_status"] == VALID and record["review"]:
            reviews[record["session_id"]] = model_class.model_validate(record["review"])
        else:
            LOGGER.info(f"{record['session_id']}: review record is {record['validation_status']}, session falls back to the baseline")
    LOGGER.info(f"{len(reviews)} valid reviews parsed from {len(records)} records")
    return reviews


def load_reviews(store: ReviewStore, sessions: list[ReviewSession], model_class: type[BaseModel]) -> dict:
    return parse_reviews(review_records_for(store, sessions), model_class)


class MomentReviewer:
    def __init__(self, client: OpenRouterClient, store: ReviewStore, dataset_id: str, prompts: ReviewPrompts | TopicPrompts, style_id: str, schema_name: str, schema: dict, parser_for: Callable[[int], Callable[[str], BaseModel]], concurrency: int = DEFAULT_REVIEW_CONCURRENCY):
        self.client = client
        self.store = store
        self.dataset_id = dataset_id
        self.prompts = prompts
        self.style_id = style_id
        self.schema_name = schema_name
        self.schema = schema
        self.parser_for = parser_for
        self.concurrency = concurrency
        self._lock = threading.Lock()
        self._done = 0

    def review(self, sessions: list[ReviewSession], limit: int | None = None, on_progress: Callable[[ReviewProgress], None] | None = None) -> ReviewRun:
        started_utc = datetime.now(timezone.utc).isoformat()
        pending = pending_sessions(self.store, sessions)
        skipped_existing = len(sessions) - len(pending)
        if limit is not None:
            LOGGER.info(f"{self.dataset_id}: limit {limit} applied to {len(pending)} pending sessions")
            pending = pending[:limit]
        LOGGER.info(f"{self.dataset_id}: reviewing {len(pending)} sessions as {self.style_id}, {skipped_existing} already stored")

        self._done = 0
        attempts = self._review_all(pending, on_progress)
        records = [attempt.record for attempt in attempts if attempt.record]
        request_failed = sum(1 for attempt in attempts if attempt.record is None)
        source_version = sessions[0].source_version if sessions else ""

        summary = ReviewSummary(
            dataset_id=self.dataset_id,
            style=self.style_id,
            model=self.client.model,
            review_prompt_version=self.prompts.version,
            review_schema_version=self._schema_version(),
            source_version=source_version,
            started_utc=started_utc,
            finished_utc=datetime.now(timezone.utc).isoformat(),
            sessions_total=len(sessions),
            sessions_skipped_existing=skipped_existing,
            sessions_reviewed=len(records),
            valid=sum(1 for record in records if record.validation_status == VALID),
            invalid=sum(1 for record in records if record.validation_status != VALID),
            request_failed=request_failed,
            total_prompt_tokens=sum(record.prompt_tokens for record in records),
            total_completion_tokens=sum(record.completion_tokens for record in records),
            total_cost_usd=sum(record.cost_usd for record in records),
        )
        if records or request_failed:
            self.store.write_summary(summary)
        else:
            LOGGER.info(f"{self.dataset_id}: no review requests sent, no summary written")
        return ReviewRun(summary=summary, records=records)

    def _schema_version(self) -> str:
        return REVIEW_SCHEMA_VERSION if self.schema_name == REVIEW_SCHEMA_NAME else TOPIC_SCHEMA_VERSION

    def _review_all(self, sessions: list[ReviewSession], on_progress: Callable[[ReviewProgress], None] | None) -> list[SessionAttempt]:
        if not sessions:
            LOGGER.info(f"{self.dataset_id}: nothing pending, no review requests sent")
            return []

        attempts = []
        with ThreadPoolExecutor(max_workers=self.concurrency) as pool:
            futures = {pool.submit(self._review_one, session): session for session in sessions}
            for future in as_completed(futures):
                session = futures[future]
                attempt = future.result()
                attempts.append(attempt)
                self._report(session, attempt, len(sessions), on_progress)
        return attempts

    def _report(self, session: ReviewSession, attempt: SessionAttempt, total: int, on_progress: Callable[[ReviewProgress], None] | None):
        with self._lock:
            self._done += 1
            done = self._done
        status = attempt.record.validation_status if attempt.record else "failed"
        if on_progress:
            on_progress(ReviewProgress(done=done, total=total, session_id=session.session_id, status=status))
        else:
            LOGGER.debug(f"{session.session_id}: no progress listener attached")

    def _review_one(self, session: ReviewSession) -> SessionAttempt:
        messages = self.prompts.build_messages(render_session(session))
        evaluator = StructuredEvaluator(self.client, self.schema_name, self.schema, self.parser_for(len(session.frames)))
        try:
            outcome = call_with_retry(session.session_id, lambda: evaluator.evaluate(session.session_id, messages))
        except OpenRouterError as error:
            detail = f"request failed with status {error.status_code}: {error}"
            LOGGER.warning(f"{session.session_id}: {detail}")
            return SessionAttempt(record=None, failure_detail=detail)

        record = self._build_record(session, outcome)
        self.store.write_record(record)
        LOGGER.info(f"{session.session_id}: {record.validation_status} attempts={record.attempts} cost=${record.cost_usd:.4f}")
        return SessionAttempt(record=record, failure_detail=None)

    def _build_record(self, session: ReviewSession, outcome: EvaluationOutcome) -> ReviewRecord:
        review = None if outcome.evaluation is None else outcome.evaluation.model_dump()
        return ReviewRecord(
            dataset_id=self.dataset_id,
            session_id=session.session_id,
            style=self.style_id,
            frame_paths=[frame.relative_path for frame in session.frames],
            model=self.client.model,
            review_prompt_version=self.prompts.version,
            review_schema_version=self._schema_version(),
            source_version=session.source_version,
            reviewed_utc=datetime.now(timezone.utc).isoformat(),
            attempts=outcome.attempts,
            latency_ms=outcome.latency_ms,
            prompt_tokens=outcome.prompt_tokens,
            completion_tokens=outcome.completion_tokens,
            cost_usd=outcome.cost_usd,
            generation_id=outcome.generation_id,
            validation_status=outcome.validation_status,
            review=review,
            failure_detail=outcome.failure_detail,
        )


def reviewer_for(client: OpenRouterClient, store: ReviewStore, dataset_id: str, style: GroupingStyle, concurrency: int = DEFAULT_REVIEW_CONCURRENCY) -> MomentReviewer:
    prompts = prompts_for(style)
    if style.kind == MOMENT_REVIEW:
        return MomentReviewer(client, store, dataset_id, prompts, style.id, REVIEW_SCHEMA_NAME, review_schema(), review_parser, concurrency)
    return MomentReviewer(client, store, dataset_id, prompts, style.id, TOPIC_SCHEMA_NAME, topic_schema(), topic_parser, concurrency)
