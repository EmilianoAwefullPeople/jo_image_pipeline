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
from jo_web.config import UPLOAD_DATASET_ID, WebConfig
from jo_web.registry import EVALUATING, EXTRACTING, GROUPING, INVENTORYING, REFINING, THUMBNAILING, RunRegistry
from llm_pipeline.client import OpenRouterClient
from llm_pipeline.discovery import discover_images
from llm_pipeline.prompts import PROMPT_VERSION
from llm_pipeline.runner import EvaluationProgress, EvaluationRunner
from llm_pipeline.schema import SCHEMA_VERSION
from llm_pipeline.store import RunStore

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

        self.registry.set_stage(run_id, THUMBNAILING, 0, len(extracted))
        self.registry.set_thumbnails(run_id, self._build_thumbnails(run_id, dataset_path, manifest))

        self.registry.set_stage(run_id, GROUPING, 0, len(extracted))
        baseline = MomentGrouper().group([build_signals(asset) for asset in extracted])
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
        signals = build_image_signals(evaluations)
        refined = ProposalRefiner().refine(baseline, signals)
        self.registry.set_groups(run_id, refined, baseline)
        LOGGER.info(f"{run_id}: refinement produced {len(refined)} proposals from {len(baseline)} baseline proposals")

    def _build_thumbnails(self, run_id: str, dataset_path: Path, manifest: DatasetManifest) -> dict:
        target_dir = self.config.thumbnail_dir(run_id)
        target_dir.mkdir(parents=True, exist_ok=True)
        thumbnails = {}
        for entry in manifest.entries:
            key = entry.sha256[:THUMBNAIL_KEY_LENGTH]
            target = target_dir / f"{key}.jpg"
            if target.is_file():
                LOGGER.debug(f"{entry.relative_path}: thumbnail {key} already written for an identical file")
                thumbnails[entry.relative_path] = key
                continue
            if not self._write_thumbnail(dataset_path / entry.relative_path, target, entry.relative_path):
                continue
            thumbnails[entry.relative_path] = key
        LOGGER.info(f"{run_id}: wrote {len(set(thumbnails.values()))} thumbnails for {len(thumbnails)} files")
        return thumbnails

    def _write_thumbnail(self, source: Path, target: Path, relative_path: str) -> bool:
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

        llm_config = self.config.llm_config(run_id)
        images = discover_images(dataset_path)
        self.registry.set_stage(run_id, EVALUATING, 0, len(images))
        store = RunStore(llm_config.llm_runs_dir, UPLOAD_DATASET_ID, PROMPT_VERSION, SCHEMA_VERSION)

        with OpenRouterClient(llm_config.openrouter_api_key, llm_config.openrouter_model, transport=self.transport) as client:
            runner = EvaluationRunner(client, store, UPLOAD_DATASET_ID, dataset_path, concurrency=self.config.llm_concurrency)
            evaluation = runner.run(images, on_progress=lambda progress: self._on_evaluation_progress(run_id, progress))

        self.registry.set_evaluation(run_id, evaluation.summary, evaluation.records)

    def _on_evaluation_progress(self, run_id: str, progress: EvaluationProgress):
        self.registry.set_stage(run_id, EVALUATING, progress.done, progress.total)
