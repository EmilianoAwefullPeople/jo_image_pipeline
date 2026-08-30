import logging
from dataclasses import asdict
from pathlib import Path

import httpx
from PIL import Image, ImageOps, UnidentifiedImageError

from jo_pipeline.assets import AssetLoader, build_signals
from jo_pipeline.cli import build_reliability, fan_out_evaluations
from jo_pipeline.group import MomentGrouper
from jo_pipeline.manifest import DatasetManifest, InventoryScanner
from jo_pipeline.refine import ProposalRefiner, build_image_signals
from jo_pipeline.regroup import Regrouper, SessionBuilder, unassigned_paths
from jo_web.config import UPLOAD_DATASET_ID, WebConfig
from jo_web.registry import EVALUATING, EXTRACTING, GROUPING, INVENTORYING, REFINING, REVIEWING, THUMBNAILING, RunRegistry, StyleResult
from llm_pipeline.client import OpenRouterClient
from llm_pipeline.discovery import discover_images
from llm_pipeline.prompts import PromptSet, default_prompts
from llm_pipeline.review import ReviewProgress, ReviewStore, parse_reviews, prompts_for, review_model_for, review_records_for, reviewer_for, schema_version_for
from llm_pipeline.runner import EvaluationProgress, EvaluationRunner
from llm_pipeline.schema import SCHEMA_VERSION
from llm_pipeline.store import RunStore, run_name
from llm_pipeline.styles import TOPIC_REVIEW, style_for

LOGGER = logging.getLogger(__name__)

DATASET_VERSION = "1"
THUMBNAIL_MAX_EDGE = 320
THUMBNAIL_QUALITY = 80
THUMBNAIL_KEY_LENGTH = 16


class PipelineService:
    def __init__(self, config: WebConfig, registry: RunRegistry, transport: httpx.BaseTransport | None = None):
        self.config = config
        self.registry = registry
        self.transport = transport

    def execute(self, run_id: str):
        pipeline_config = self.config.pipeline_config(run_id)
        dataset_path = pipeline_config.dataset_path(UPLOAD_DATASET_ID)

        self.registry.set_stage(run_id, INVENTORYING, 0, 0)
        scanner = InventoryScanner(pipeline_config.dataset_root, pipeline_config.manifest_dir)
        manifest = scanner.scan(UPLOAD_DATASET_ID, DATASET_VERSION)
        scanner.write(manifest)

        self.registry.set_stage(run_id, EXTRACTING, 0, len(manifest.entries))
        assets = AssetLoader(dataset_path).load(manifest)
        extracted = [asset for asset in assets if not asset.failure]
        report = build_reliability(assets)
        self.registry.set_extraction(run_id, report.rows(), report.failures, report.assets)
        self.registry.set_assets(run_id, assets)

        self.registry.set_stage(run_id, THUMBNAILING, 0, len(extracted))
        self.registry.set_thumbnails(run_id, self._build_thumbnails(run_id, dataset_path, manifest))

        self.registry.set_stage(run_id, GROUPING, 0, len(extracted))
        signals = [build_signals(asset) for asset in extracted]
        baseline = MomentGrouper().group(signals)
        self.registry.set_signals(run_id, signals)
        self.registry.set_groups(run_id, baseline, baseline)

        self._evaluate(run_id, dataset_path)
        self._refine(run_id, manifest, baseline)

    def _refine(self, run_id: str, manifest: DatasetManifest, baseline: list):
        state = self.registry.get(run_id)
        if state is None or not state.llm_records:
            LOGGER.info(f"{run_id}: refinement skipped, no evaluation records for this run")
            return

        self.registry.set_stage(run_id, REFINING, 0, len(baseline))
        evaluations = fan_out_evaluations(manifest, [asdict(record) for record in state.llm_records])
        image_signals = build_image_signals(state.llm_records[0].schema_version, evaluations)
        refined = ProposalRefiner().refine(baseline, image_signals)
        self.registry.set_groups(run_id, refined, baseline)
        LOGGER.info(f"{run_id}: refinement produced {len(refined)} proposals from {len(baseline)} baseline proposals")
        self.group_by_style(run_id)

    def group_by_style(self, run_id: str):
        state = self.registry.get(run_id)
        if state is None or not state.llm_records:
            LOGGER.info(f"{run_id}: grouping by style skipped, no evaluation records for this run")
            return

        style = style_for(state.style)
        manifest = InventoryScanner(self.config.pipeline_config(run_id).dataset_root, self.config.pipeline_config(run_id).manifest_dir).load(UPLOAD_DATASET_ID, DATASET_VERSION)
        evaluations = fan_out_evaluations(manifest, [asdict(record) for record in state.llm_records])
        image_signals = build_image_signals(state.llm_records[0].schema_version, evaluations)
        signals_by_path = {signal.relative_path: signal for signal in state.signals}
        baseline = state.baseline_groups
        source_version = run_name(state.llm_summary.prompt_version, state.llm_summary.schema_version)
        sessions = SessionBuilder.for_style(style).build(UPLOAD_DATASET_ID, source_version, prompts_for(style).version, schema_version_for(style), baseline, signals_by_path, image_signals)
        if not sessions:
            LOGGER.info(f"{run_id}: {style.id} review skipped, no outing holds more than one photo")
            return

        llm_config = self.config.llm_config(run_id)
        store = ReviewStore.for_style(llm_config.llm_runs_dir, UPLOAD_DATASET_ID, style)
        self.registry.set_stage(run_id, REVIEWING, 0, len(sessions))
        with OpenRouterClient(llm_config.openrouter_api_key, llm_config.openrouter_model, transport=self.transport) as client:
            reviewer = reviewer_for(client, store, UPLOAD_DATASET_ID, style, concurrency=self.config.llm_concurrency)
            run = reviewer.review(sessions, on_progress=lambda progress: self._on_review_progress(run_id, progress))

        records = review_records_for(store, sessions)
        reviews = parse_reviews(records, review_model_for(style))
        grouped = Regrouper().regroup(baseline, signals_by_path, image_signals, sessions, reviews, style)
        unassigned = unassigned_paths(sessions, reviews) if style.kind == TOPIC_REVIEW else []
        self.registry.set_review(run_id, run.summary, records, unassigned)
        self.registry.set_groups(run_id, grouped, baseline)
        self.registry.set_style_result(run_id, StyleResult(style=style.id, groups=grouped, summary=run.summary, records=records, unassigned=unassigned))
        LOGGER.info(f"{run_id}: {style.id} produced {len(grouped)} groups from {len(baseline)} baseline proposals across {len(sessions)} sessions")

    def _on_review_progress(self, run_id: str, progress: ReviewProgress):
        self.registry.set_stage(run_id, REVIEWING, progress.done, progress.total)

    def _build_thumbnails(self, run_id: str, dataset_path: Path, manifest: DatasetManifest) -> dict:
        target_dir = self.config.thumbnail_dir(run_id)
        thumbnails = {}
        for entry in manifest.entries:
            key = entry.sha256[:THUMBNAIL_KEY_LENGTH]
            if self.ensure_thumbnail(dataset_path / entry.relative_path, target_dir / f"{key}.jpg", entry.relative_path):
                thumbnails[entry.relative_path] = key
        LOGGER.info(f"{run_id}: {len(set(thumbnails.values()))} thumbnails on disk for {len(thumbnails)} files")
        return thumbnails

    def ensure_thumbnail(self, source: Path, target: Path, relative_path: str) -> bool:
        if target.is_file():
            LOGGER.debug(f"{relative_path}: thumbnail {target.stem} already on disk")
            return True

        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            with Image.open(source) as image:
                upright = ImageOps.exif_transpose(image)
                sample = upright.convert("RGB")
                sample.thumbnail((THUMBNAIL_MAX_EDGE, THUMBNAIL_MAX_EDGE), Image.Resampling.LANCZOS)
                sample.save(target, format="JPEG", quality=THUMBNAIL_QUALITY)
        except (OSError, UnidentifiedImageError, ValueError, Image.DecompressionBombError) as error:
            LOGGER.warning(f"{relative_path}: thumbnail failed with {type(error).__name__} {error}")
            return False
        LOGGER.debug(f"{relative_path}: thumbnail written to {target.name}")
        return True

    def _evaluate(self, run_id: str, dataset_path: Path):
        if not self.config.openrouter_api_key:
            LOGGER.warning(f"{run_id}: evaluation skipped, JO_OPENROUTER_API_KEY is not set")
            return

        prompts = self._prompts(run_id)
        llm_config = self.config.llm_config(run_id)
        images = discover_images(dataset_path)
        self.registry.set_stage(run_id, EVALUATING, 0, len(images))
        store = RunStore.for_versions(llm_config.llm_runs_dir, UPLOAD_DATASET_ID, prompts.version, SCHEMA_VERSION)

        with OpenRouterClient(llm_config.openrouter_api_key, llm_config.openrouter_model, transport=self.transport) as client:
            runner = EvaluationRunner(client, store, UPLOAD_DATASET_ID, dataset_path, prompts, concurrency=self.config.llm_concurrency)
            evaluation = runner.run(images, on_progress=lambda progress: self._on_evaluation_progress(run_id, progress))

        self.registry.set_evaluation(run_id, evaluation.summary, evaluation.records)

    def _prompts(self, run_id: str) -> PromptSet:
        state = self.registry.get(run_id)
        if state is None or state.prompts is None:
            LOGGER.info(f"{run_id}: evaluating with the stored default prompt")
            return default_prompts()
        LOGGER.info(f"{run_id}: evaluating with the {state.prompts.version} prompt supplied for this run")
        return state.prompts

    def _on_evaluation_progress(self, run_id: str, progress: EvaluationProgress):
        self.registry.set_stage(run_id, EVALUATING, progress.done, progress.total)
