import json
import logging
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from jo_pipeline.assets import LoadedAsset
from jo_pipeline.config import PROMPT_VERSION, SCHEMA_VERSION
from jo_pipeline.group import GroupProposal
from jo_pipeline.manifest import DatasetManifest
from jo_pipeline.normalize import MetadataObservation

LOGGER = logging.getLogger(__name__)

RUNNING = "running"
COMPLETE = "complete"
FAILED = "failed"

OPENROUTER_PROVIDER = "openrouter"


@dataclass(frozen=True)
class ModelCall:
    relative_path: str
    provider: str
    model_id: str
    prompt_version: str
    schema_version: str
    attempt: int
    validation_status: str
    latency_ms: int
    prompt_tokens: int
    completion_tokens: int
    cost_usd: float
    parsed_output: dict | None
    failure_detail: str | None
    called_utc: str

INSERT_DATASET = """
insert into datasets (id, source_root, created_utc) values (?, ?, ?)
on conflict (id) do update set source_root = excluded.source_root
"""
INSERT_ASSET = """
insert into media_assets (dataset_id, relative_path, media_type, size_bytes, sha256, modified_utc) values (?, ?, ?, ?, ?, ?)
on conflict (dataset_id, relative_path) do update set media_type = excluded.media_type, size_bytes = excluded.size_bytes, sha256 = excluded.sha256, modified_utc = excluded.modified_utc
"""
INSERT_RUN = """
insert into processing_runs (dataset_id, dataset_version, manifest_sha256, schema_version, prompt_version, model_id, status, started_utc)
values (?, ?, ?, ?, ?, ?, ?, ?)
"""
INSERT_OBSERVATION = """
insert into metadata_observations (media_asset_id, processing_run_id, category, field, value, raw_value, source, method_version, confidence, evidence, unknown_reason, recorded_utc)
values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
"""
INSERT_PROPOSAL = """
insert into group_proposals (processing_run_id, dataset_id, label, start_utc, end_utc, place_label, score, method_version, evidence)
values (?, ?, ?, ?, ?, ?, ?, ?, ?)
"""
INSERT_MEMBER = "insert into group_members (group_proposal_id, media_asset_id, membership, evidence) values (?, ?, ?, ?)"
INSERT_MODEL_CALL = """
insert into model_calls (media_asset_id, processing_run_id, provider, model_id, prompt_version, schema_version, attempt, validation_status, request_bytes, latency_ms, prompt_tokens, completion_tokens, cost_usd, raw_response, parsed_output, failure_detail, called_utc)
values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
"""


class PipelineStore:
    def __init__(self, database_path: Path):
        if not database_path.is_file():
            raise FileNotFoundError(f"database not found at {database_path}, run scripts/init_db.py to create it")

        self.database_path = database_path
        self.connection = None

    def __enter__(self) -> "PipelineStore":
        self.connection = sqlite3.connect(self.database_path)
        self.connection.execute("pragma foreign_keys = on")
        return self

    def __exit__(self, exception_type, exception, traceback):
        if exception_type:
            LOGGER.warning(f"rolling back the pipeline transaction after {exception_type.__name__}")
            self.connection.rollback()
        else:
            self.connection.commit()
        self.connection.close()
        self.connection = None

    def save_dataset(self, manifest: DatasetManifest):
        self.connection.execute(INSERT_DATASET, (manifest.dataset_id, manifest.source_root, utc_now()))
        LOGGER.debug(f"{manifest.dataset_id}: dataset record stored")

    def save_assets(self, manifest: DatasetManifest) -> dict:
        for entry in manifest.entries:
            self.connection.execute(INSERT_ASSET, (
                manifest.dataset_id,
                entry.relative_path,
                entry.media_type,
                entry.size_bytes,
                entry.sha256,
                entry.modified_utc,
            ))

        rows = self.connection.execute("select relative_path, id from media_assets where dataset_id = ?", (manifest.dataset_id,)).fetchall()
        LOGGER.info(f"{manifest.dataset_id}: stored {len(manifest.entries)} media assets")
        return {relative_path: identifier for relative_path, identifier in rows}

    def start_run(self, manifest: DatasetManifest, manifest_sha256: str, model_id: str | None) -> int:
        cursor = self.connection.execute(INSERT_RUN, (
            manifest.dataset_id,
            manifest.dataset_version,
            manifest_sha256,
            SCHEMA_VERSION,
            PROMPT_VERSION,
            model_id,
            RUNNING,
            utc_now(),
        ))
        LOGGER.info(f"{manifest.dataset_id}: processing run {cursor.lastrowid} started against manifest {manifest_sha256[:12]}")
        return cursor.lastrowid

    def complete_run(self, run_id: int, status: str, failure_detail: str | None = None):
        self.connection.execute(
            "update processing_runs set status = ?, finished_utc = ?, failure_detail = ? where id = ?",
            (status, utc_now(), failure_detail, run_id),
        )
        LOGGER.info(f"{run_id}: processing run finished with status {status}")

    def save_observations(self, run_id: int, asset_ids: dict, assets: list[LoadedAsset]) -> int:
        stored = 0
        for asset in assets:
            asset_id = asset_ids[asset.entry.relative_path]
            for observation in asset.observations:
                self._insert_observation(asset_id, run_id, observation)
                stored += 1

        LOGGER.info(f"{run_id}: stored {stored} metadata observations")
        return stored

    def save_path_observations(self, run_id: int, asset_ids: dict, observations: dict) -> int:
        stored = 0
        for relative_path, rows in observations.items():
            asset_id = asset_ids.get(relative_path)
            if asset_id is None:
                LOGGER.warning(f"{relative_path}: observations skipped, no stored media asset for this path")
                continue

            for observation in rows:
                self._insert_observation(asset_id, run_id, observation)
                stored += 1

        LOGGER.info(f"{run_id}: stored {stored} observations across {len(observations)} paths")
        return stored

    def save_model_calls(self, run_id: int, asset_ids: dict, calls: list[ModelCall]) -> int:
        stored = 0
        for call in calls:
            asset_id = asset_ids.get(call.relative_path)
            if asset_id is None:
                LOGGER.warning(f"{call.relative_path}: model call skipped, no stored media asset for this path")
                continue

            self.connection.execute(INSERT_MODEL_CALL, (
                asset_id,
                run_id,
                call.provider,
                call.model_id,
                call.prompt_version,
                call.schema_version,
                call.attempt,
                call.validation_status,
                None,
                call.latency_ms,
                call.prompt_tokens,
                call.completion_tokens,
                call.cost_usd,
                None,
                encode(call.parsed_output),
                call.failure_detail,
                call.called_utc,
            ))
            stored += 1

        LOGGER.info(f"{run_id}: stored {stored} model calls")
        return stored

    def _insert_observation(self, asset_id: int, run_id: int, observation: MetadataObservation):
        self.connection.execute(INSERT_OBSERVATION, (
            asset_id,
            run_id,
            observation.category,
            observation.field,
            encode(observation.value),
            encode(observation.raw_value),
            observation.source,
            observation.method_version,
            observation.confidence,
            encode(observation.evidence),
            observation.unknown_reason,
            utc_now(),
        ))

    def save_proposals(self, run_id: int, dataset_id: str, asset_ids: dict, proposals: list[GroupProposal]) -> int:
        stored = 0
        for proposal in proposals:
            cursor = self.connection.execute(INSERT_PROPOSAL, (
                run_id,
                dataset_id,
                proposal.label,
                proposal.start_utc,
                proposal.end_utc,
                None,
                proposal.score,
                proposal.method_version,
                encode(proposal.evidence),
            ))
            self._save_members(cursor.lastrowid, asset_ids, proposal)
            stored += 1

        LOGGER.info(f"{run_id}: stored {stored} group proposals")
        return stored

    def _save_members(self, proposal_id: int, asset_ids: dict, proposal: GroupProposal):
        for member in proposal.members:
            asset_id = asset_ids.get(member.relative_path)
            if asset_id is None:
                LOGGER.warning(f"{member.relative_path}: member skipped, no stored media asset for this path")
                continue

            self.connection.execute(INSERT_MEMBER, (proposal_id, asset_id, member.membership, encode(member.evidence)))


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def encode(value: object) -> str | None:
    if value is None:
        return None
    return json.dumps(value)
