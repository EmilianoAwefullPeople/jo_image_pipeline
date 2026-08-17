import argparse
import json
import logging
from dataclasses import asdict, dataclass
from pathlib import Path

from jo_pipeline.assets import AssetLoader, build_signals
from jo_pipeline.config import PipelineConfig, load_config, resolve_path
from jo_pipeline.group import BURST, DUPLICATE, MomentGrouper
from jo_pipeline.keepscore import KeepSignalScore, KeepSignalScorer
from jo_pipeline.logging_setup import configure_logging
from jo_pipeline.manifest import DatasetManifest, InventoryScanner, manifest_digest
from jo_pipeline.persist import COMPLETE, OPENROUTER_PROVIDER, ModelCall, PipelineStore
from jo_pipeline.reference import ReferenceGrouping, ReferenceReader
from jo_pipeline.refine import ProposalRefiner, build_image_signals, build_llm_observations
from jo_pipeline.reliability import ReliabilityReport
from jo_pipeline.review import GroupingReviewer, ReviewComparison
from llm_pipeline.prompts import PROMPT_VERSION as LLM_PROMPT_VERSION
from llm_pipeline.schema import SCHEMA_VERSION as LLM_SCHEMA_VERSION
from llm_pipeline.store import RunStore

LOGGER = logging.getLogger(__name__)

REFERENCE_PATTERN = "**/*.docx"
COLLAPSED_MEMBERSHIPS = (DUPLICATE, BURST)
LLM_METHOD_VERSION = f"p{LLM_PROMPT_VERSION}/{LLM_SCHEMA_VERSION}"


def load_manifest(config: PipelineConfig, args: argparse.Namespace) -> DatasetManifest:
    return InventoryScanner(config.dataset_root, config.manifest_dir).load(args.dataset, args.dataset_version)


def ensure_manifest(config: PipelineConfig, args: argparse.Namespace) -> DatasetManifest:
    scanner = InventoryScanner(config.dataset_root, config.manifest_dir)
    if scanner.manifest_path(args.dataset, args.dataset_version).is_file():
        LOGGER.info(f"{args.dataset}: reusing manifest v{args.dataset_version}")
        return scanner.load(args.dataset, args.dataset_version)

    manifest = scanner.scan(args.dataset, args.dataset_version)
    scanner.write(manifest)
    return manifest


def load_assets(config: PipelineConfig, args: argparse.Namespace) -> list:
    return AssetLoader(config.dataset_path(args.dataset)).load(load_manifest(config, args))


def build_reliability(assets: list) -> ReliabilityReport:
    report = ReliabilityReport()
    for asset in assets:
        if asset.failure:
            report.add_failure(asset.entry.relative_path, asset.failure)
        else:
            report.add_asset(asset.entry.relative_path, asset.observations)
    return report


def find_reference_document(dataset_path: Path, override: str | None) -> Path | None:
    if override:
        return resolve_path(override)

    documents = sorted(dataset_path.glob(REFERENCE_PATTERN))
    if not documents:
        return None

    LOGGER.info(f"using reference document {documents[0]}")
    return documents[0]


@dataclass(frozen=True)
class RunArtifacts:
    manifest: DatasetManifest
    extracted: list
    baseline: list
    refined: list
    model_calls: list
    llm_observations: dict
    model_id: str | None


def load_llm_records(config: PipelineConfig, dataset_id: str) -> list[dict]:
    return RunStore(config.llm_runs_dir, dataset_id, LLM_PROMPT_VERSION, LLM_SCHEMA_VERSION).records()


def fan_out_evaluations(manifest: DatasetManifest, records: list[dict]) -> dict:
    by_hash = {record["sha256"]: record for record in records}
    evaluations = {}
    for entry in manifest.entries:
        record = by_hash.get(entry.sha256)
        if record is None:
            LOGGER.debug(f"{entry.relative_path}: no evaluation record for hash {entry.sha256[:12]}")
            continue
        evaluations[entry.relative_path] = record["evaluation"]

    LOGGER.info(f"{manifest.dataset_id}: {len(evaluations)} of {len(manifest.entries)} files carry an evaluation from {len(records)} records")
    return evaluations


def build_model_calls(records: list[dict]) -> list[ModelCall]:
    return [ModelCall(
        relative_path=record["relative_path"],
        provider=OPENROUTER_PROVIDER,
        model_id=record["model"],
        prompt_version=record["prompt_version"],
        schema_version=record["schema_version"],
        attempt=record["attempts"],
        validation_status=record["validation_status"],
        latency_ms=record["latency_ms"],
        prompt_tokens=record["prompt_tokens"],
        completion_tokens=record["completion_tokens"],
        cost_usd=record["cost_usd"],
        parsed_output=record["evaluation"],
        failure_detail=record["failure_detail"],
        called_utc=record["evaluated_utc"],
    ) for record in records]


def build_llm_observation_rows(evaluations: dict, model_id: str) -> dict:
    rows = {}
    for relative_path, evaluation in evaluations.items():
        if evaluation:
            rows[relative_path] = build_llm_observations(evaluation, model_id, LLM_METHOD_VERSION)
        else:
            LOGGER.info(f"{relative_path}: evaluation failed validation, no observations to store")
    return rows


def build_run_artifacts(config: PipelineConfig, args: argparse.Namespace, manifest: DatasetManifest) -> RunArtifacts:
    assets = AssetLoader(config.dataset_path(args.dataset)).load(manifest)
    extracted = [asset for asset in assets if not asset.failure]
    baseline = MomentGrouper().group([build_signals(asset) for asset in extracted])

    records = load_llm_records(config, args.dataset)
    evaluations = fan_out_evaluations(manifest, records)
    image_signals = build_image_signals(evaluations)
    refined = ProposalRefiner().refine(baseline, image_signals)
    model_id = records[0]["model"] if records else None

    return RunArtifacts(
        manifest=manifest,
        extracted=extracted,
        baseline=baseline,
        refined=refined,
        model_calls=build_model_calls(records),
        llm_observations=build_llm_observation_rows(evaluations, model_id) if model_id else {},
        model_id=model_id,
    )


def persist_run(config: PipelineConfig, artifacts: RunArtifacts) -> int:
    manifest = artifacts.manifest
    with PipelineStore(config.database_path) as store:
        store.save_dataset(manifest)
        asset_ids = store.save_assets(manifest)
        run_id = store.start_run(manifest, manifest_digest(manifest), artifacts.model_id)
        store.save_observations(run_id, asset_ids, artifacts.extracted)
        store.save_proposals(run_id, manifest.dataset_id, asset_ids, artifacts.baseline)
        if artifacts.model_calls:
            store.save_model_calls(run_id, asset_ids, artifacts.model_calls)
            store.save_path_observations(run_id, asset_ids, artifacts.llm_observations)
            store.save_proposals(run_id, manifest.dataset_id, asset_ids, artifacts.refined)
        else:
            LOGGER.info(f"{manifest.dataset_id}: no evaluation records, storing the deterministic baseline only")
        store.complete_run(run_id, COMPLETE)
    return run_id


def run_pipeline(config: PipelineConfig, args: argparse.Namespace):
    manifest = ensure_manifest(config, args)
    assets = AssetLoader(config.dataset_path(args.dataset)).load(manifest)
    extracted = [asset for asset in assets if not asset.failure]
    signals = [build_signals(asset) for asset in extracted]
    proposals = MomentGrouper().group(signals)

    print(f"\n{'=' * 78}\n{args.dataset}  manifest v{args.dataset_version}  {manifest_digest(manifest)[:12]}\n{'=' * 78}")
    print(f"\n{len(manifest.entries)} files inventoried from {manifest.source_root}")
    print_reliability(args.dataset, build_reliability(assets))
    print_group_summary(proposals)
    print_pipeline_review(config, args, proposals, signals)
    print_pipeline_persistence(config, args, manifest, extracted, proposals)


def run_inventory(config: PipelineConfig, args: argparse.Namespace):
    scanner = InventoryScanner(config.dataset_root, config.manifest_dir)
    manifest = scanner.scan(args.dataset, args.dataset_version)
    scanner.write(manifest)
    for entry in manifest.entries:
        LOGGER.info(f"{args.dataset}: {entry.relative_path} {entry.media_type} {entry.size_bytes} bytes {entry.sha256[:12]}")


def run_extract(config: PipelineConfig, args: argparse.Namespace):
    print_reliability(args.dataset, build_reliability(load_assets(config, args)))


def run_group(config: PipelineConfig, args: argparse.Namespace):
    signals = [build_signals(asset) for asset in load_assets(config, args) if not asset.failure]
    proposals = MomentGrouper().group(signals)
    print_groups(args.dataset, signals, proposals)


def run_persist(config: PipelineConfig, args: argparse.Namespace):
    artifacts = build_run_artifacts(config, args, load_manifest(config, args))
    run_id = persist_run(config, artifacts)
    proposals = len(artifacts.baseline) + (len(artifacts.refined) if artifacts.model_calls else 0)
    print(f"\n{args.dataset}: run {run_id} stored {len(artifacts.manifest.entries)} assets, {proposals} proposals and {len(artifacts.model_calls)} model calls in {config.database_path}\n")


def run_review(config: PipelineConfig, args: argparse.Namespace):
    signals = [build_signals(asset) for asset in load_assets(config, args) if not asset.failure]
    proposals = MomentGrouper().group(signals)
    document = find_reference_document(config.dataset_path(args.dataset), args.reference)
    if not document:
        raise FileNotFoundError(f"no reference document found under {config.dataset_path(args.dataset)}, pass --reference with a path")

    reference = ReferenceReader(document).read(args.dataset, signals)
    comparison = GroupingReviewer().compare(proposals, reference)
    artifact = write_review_artifact(config, args, proposals, reference, comparison)
    print_review(args.dataset, reference, comparison, artifact)


def run_benchmark(config: PipelineConfig, args: argparse.Namespace):
    manifest = ensure_manifest(config, args)
    artifacts = build_run_artifacts(config, args, manifest)
    signals = [build_signals(asset) for asset in artifacts.extracted]
    evaluations = fan_out_evaluations(manifest, load_llm_records(config, args.dataset))
    image_signals = build_image_signals(evaluations)

    print(f"\n{'=' * 78}\n{args.dataset}  benchmark  model {artifacts.model_id or 'none'}\n{'=' * 78}")
    print(f"\n{len(image_signals)} of {len(signals)} extracted images carry a valid evaluation\n")
    print_refinement_effect(artifacts)

    document = find_reference_document(config.dataset_path(args.dataset), args.reference)
    if not document:
        print("Reference: no reference grouping supplied for this dataset, accuracy not scored\n")
        return

    reference = ReferenceReader(document).read(args.dataset, signals)
    baseline_comparison = GroupingReviewer().compare(artifacts.baseline, reference)
    refined_comparison = GroupingReviewer().compare(artifacts.refined, reference)
    keep = KeepSignalScorer().score(reference, image_signals)
    print_benchmark(baseline_comparison, refined_comparison, keep)

    artifact = write_benchmark_artifact(config, args, artifacts, baseline_comparison, refined_comparison, keep)
    print(f"  artifact                   {artifact}\n")


def print_refinement_effect(artifacts: RunArtifacts):
    excluded = [entry for proposal in artifacts.refined for entry in proposal.evidence.get("excluded_by_signal", [])]
    flagged = [entry for proposal in artifacts.refined for entry in proposal.evidence.get("screenshots_flagged", [])]
    reelected = sum(
        1 for proposal in artifacts.refined
        for member in proposal.members
        if member.membership == "representative" and member.evidence.get("previous_representative") not in (None, member.relative_path)
    )
    print(f"Refinement: {len(artifacts.baseline)} baseline proposals to {len(artifacts.refined)} refined\n")
    print(f"  photos dropped as leave_out   {len(excluded)}")
    print(f"  representatives re-elected    {reelected} of {len(artifacts.refined)}")
    print(f"  screenshots flagged, retained {len(flagged)}")
    print(f"  model calls recorded          {len(artifacts.model_calls)}\n")


def print_benchmark(baseline: ReviewComparison, refined: ReviewComparison, keep: KeepSignalScore):
    print(f"Accuracy against the traveler's own grouping, baseline against refined\n")
    print(f"  {'metric':<26} {'baseline':>9} {'refined':>9}  delta")
    for label, left, right in (
        ("pair precision", baseline.pair_precision, refined.pair_precision),
        ("pair recall", baseline.pair_recall, refined.pair_recall),
        ("pair f1", baseline.pair_f1, refined.pair_f1),
    ):
        print(f"  {label:<26} {left:>9.3f} {right:>9.3f}  {right - left:+.3f}")
    for label, left, right in (
        ("proposals", baseline.proposed_groups, refined.proposed_groups),
        ("reference memories split", baseline.split_groups, refined.split_groups),
        ("proposals merging memories", baseline.merged_proposals, refined.merged_proposals),
        ("excluded photos grouped", baseline.excluded_assets_grouped, refined.excluded_assets_grouped),
    ):
        print(f"  {label:<26} {left:>9} {right:>9}  {right - left:+d}")

    print(f"\nKeep signal against the {keep.excluded_total} photos the traveler left out\n")
    print(f"  evaluated exclusions       {keep.excluded_with_signal} of {keep.excluded_total}")
    print(f"  caught as leave_out        {keep.excluded_caught}")
    print(f"  recall                     {keep.recall:.2f}")
    print(f"  false positives            {keep.grouped_false_positives} of {keep.grouped_with_signal} kept photos")
    print(f"  false positive rate        {keep.false_positive_rate:.2f}")
    for hit in keep.missed:
        print(f"  missed  {hit.relative_path}  said {hit.keep_signal} at {hit.confidence}")
    for hit in keep.false_positives:
        print(f"  false   {hit.relative_path}  {hit.reason}")


def write_benchmark_artifact(config: PipelineConfig, args: argparse.Namespace, artifacts: RunArtifacts, baseline: ReviewComparison, refined: ReviewComparison, keep: KeepSignalScore) -> Path:
    config.runs_dir.mkdir(parents=True, exist_ok=True)
    artifact = config.runs_dir / f"{args.dataset}-v{args.dataset_version}-benchmark.json"
    artifact.write_text(json.dumps({
        "dataset_id": args.dataset,
        "dataset_version": args.dataset_version,
        "model_id": artifacts.model_id,
        "llm_method_version": LLM_METHOD_VERSION,
        "baseline_comparison": baseline.as_dict(),
        "refined_comparison": refined.as_dict(),
        "keep_signal_score": keep.as_dict(),
        "baseline_proposals": [asdict(proposal) for proposal in artifacts.baseline],
        "refined_proposals": [asdict(proposal) for proposal in artifacts.refined],
    }, indent=2))
    LOGGER.info(f"{args.dataset}: benchmark artifact written to {artifact}")
    return artifact


def write_review_artifact(config: PipelineConfig, args: argparse.Namespace, proposals: list, reference: ReferenceGrouping, comparison: ReviewComparison) -> Path:
    config.runs_dir.mkdir(parents=True, exist_ok=True)
    artifact = config.runs_dir / f"{args.dataset}-v{args.dataset_version}-review.json"
    artifact.write_text(json.dumps({
        "dataset_id": args.dataset,
        "dataset_version": args.dataset_version,
        "comparison": comparison.as_dict(),
        "reference": asdict(reference),
        "proposals": [asdict(proposal) for proposal in proposals],
    }, indent=2))
    LOGGER.info(f"{args.dataset}: review artifact written to {artifact}")
    return artifact


def print_reliability(dataset_id: str, report: ReliabilityReport):
    LOGGER.info(f"{dataset_id}: extracted {report.assets} images with {len(report.failures)} failures")
    print(f"\nExtraction: {report.assets} images, {len(report.failures)} failures\n")
    print(f"  {'category':<10} {'field':<24} {'present':>9} {'rate':>7}  common unknown reason")
    for row in report.rows():
        leading_reason = max(row.unknown_reasons, key=row.unknown_reasons.get, default="")
        print(f"  {row.category:<10} {row.field:<24} {row.present:>4}/{row.total:<4} {row.presence_rate:>6.0%}  {leading_reason}")

    for relative_path, detail in report.failures.items():
        print(f"  failure  {relative_path}  {detail}")


def print_group_summary(proposals: list):
    singletons = sum(1 for proposal in proposals if len(proposal.members) == 1)
    collapsed = sum(1 for proposal in proposals for member in proposal.members if member.membership in COLLAPSED_MEMBERSHIPS)
    largest = max(len(proposal.members) for proposal in proposals)
    print(f"\nGrouping: {len(proposals)} proposals, {singletons} single photo, largest {largest} photos, {collapsed} duplicates or bursts collapsed\n")
    for index, proposal in enumerate(proposals, start=1):
        located = proposal.evidence.get("located_members", 0)
        unlocated = proposal.evidence.get("unlocated_members", 0)
        print(f"  {index:>3}. {proposal.label:<34} {len(proposal.members):>3} photos  score {proposal.score:.2f}  {located} located, {unlocated} unlocated")


def print_pipeline_review(config: PipelineConfig, args: argparse.Namespace, proposals: list, signals: list):
    document = find_reference_document(config.dataset_path(args.dataset), args.reference)
    if not document:
        print("\nReview: no reference grouping supplied for this dataset\n")
        return

    reference = ReferenceReader(document).read(args.dataset, signals)
    comparison = GroupingReviewer().compare(proposals, reference)
    artifact = write_review_artifact(config, args, proposals, reference, comparison)
    print_review(args.dataset, reference, comparison, artifact)


def print_pipeline_persistence(config: PipelineConfig, args: argparse.Namespace, manifest: DatasetManifest, extracted: list, proposals: list):
    if not config.database_path.is_file():
        print(f"Storage: skipped, no database at {config.database_path}. Run scripts/init_db.py to store runs\n")
        return

    run_id = persist_run(config, build_run_artifacts(config, args, manifest))
    print(f"Storage: run {run_id} written to {config.database_path}\n")


def print_groups(dataset_id: str, signals: list, proposals: list):
    print(f"\n{dataset_id}: {len(signals)} images grouped into {len(proposals)} proposals\n")
    for index, proposal in enumerate(proposals, start=1):
        located = proposal.evidence.get("located_members", 0)
        unlocated = proposal.evidence.get("unlocated_members", 0)
        attached = proposal.evidence.get("attached_count", 0)
        distance = proposal.evidence.get("max_distance_metres")
        spread = f"{distance:.0f}m spread" if distance else "no distance evidence"
        composition = f"{located} located, {unlocated} unlocated, {attached} attached"
        print(f"{index:>3}. {proposal.label:<34} {len(proposal.members):>3} photos  score {proposal.score:.2f}  {composition}, {spread}")
        for member in proposal.members:
            if member.membership != "member":
                print(f"     {member.membership:<14} {member.relative_path}  {member.evidence}")


def print_review(dataset_id: str, reference: ReferenceGrouping, comparison: ReviewComparison, artifact: Path):
    print(f"\nReview: {comparison.reference_groups} reference memories against {comparison.proposed_groups} proposals\n")
    print(f"  assets compared            {comparison.compared_assets}")
    print(f"  pair precision             {comparison.pair_precision:.2f}")
    print(f"  pair recall                {comparison.pair_recall:.2f}")
    print(f"  pair f1                    {comparison.pair_f1:.2f}")
    print(f"  reference memories split   {comparison.split_groups}")
    print(f"  proposals merging memories {comparison.merged_proposals}")
    print(f"  excluded photos grouped    {comparison.excluded_assets_grouped} of {len(reference.excluded_paths)}")
    print(f"  reference images unmatched {len(reference.unmatched_media)}")
    print(f"  artifact                   {artifact}\n")


def add_dataset_arguments(parser: argparse.ArgumentParser):
    parser.add_argument("--dataset", required=True, help="Dataset folder name under the dataset root")
    parser.add_argument("--dataset-version", default="1", help="Manifest version")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="jo-pipeline", description="Journey Onward media metadata and grouping pipeline")
    subparsers = parser.add_subparsers(dest="command", required=True)

    pipeline = subparsers.add_parser("run", help="Run the whole pipeline on a dataset and report the result")
    add_dataset_arguments(pipeline)
    pipeline.add_argument("--reference", help="Path to the reference document, discovered under the dataset folder when omitted")
    pipeline.set_defaults(handler=run_pipeline)

    inventory = subparsers.add_parser("inventory", help="Scan a dataset folder and write a versioned manifest")
    add_dataset_arguments(inventory)
    inventory.set_defaults(handler=run_inventory)

    extract = subparsers.add_parser("extract", help="Extract and normalize metadata for a manifested dataset")
    add_dataset_arguments(extract)
    extract.set_defaults(handler=run_extract)

    group = subparsers.add_parser("group", help="Propose moment groups for a manifested dataset")
    add_dataset_arguments(group)
    group.set_defaults(handler=run_group)

    persist = subparsers.add_parser("persist", help="Store assets, observations and group proposals for a run")
    add_dataset_arguments(persist)
    persist.set_defaults(handler=run_persist)

    review = subparsers.add_parser("review", help="Compare proposed groups against the supplied reference grouping")
    add_dataset_arguments(review)
    review.add_argument("--reference", help="Path to the reference document, discovered under the dataset folder when omitted")
    review.set_defaults(handler=run_review)

    benchmark = subparsers.add_parser("benchmark", help="Score the deterministic baseline against the visual model refinement, no API calls")
    add_dataset_arguments(benchmark)
    benchmark.add_argument("--reference", help="Path to the reference document, discovered under the dataset folder when omitted")
    benchmark.set_defaults(handler=run_benchmark)

    return parser


def main():
    args = build_parser().parse_args()
    config = load_config()
    configure_logging(config.log_level)
    LOGGER.debug(f"command {args.command} starting against dataset root {config.dataset_root}")
    args.handler(config, args)
