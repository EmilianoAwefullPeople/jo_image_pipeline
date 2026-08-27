import argparse
import json
import logging
from dataclasses import asdict, dataclass
from pathlib import Path

from jo_pipeline.assets import AssetLoader, build_signals
from jo_pipeline.config import PipelineConfig, load_config, resolve_path
from jo_pipeline.evaluation_readers import SCHEMA_V3
from jo_pipeline.group import BURST, DUPLICATE, MomentGrouper
from jo_pipeline.keepscore import KeepSignalScore, KeepSignalScorer
from jo_pipeline.logging_setup import configure_logging
from jo_pipeline.manifest import DatasetManifest, InventoryScanner, manifest_digest
from jo_pipeline.persist import COMPLETE, OPENROUTER_PROVIDER, ModelCall, PipelineStore
from jo_pipeline.reference import ReferenceGrouping, reference_reader
from jo_pipeline.refine import ProposalRefiner, build_image_signals, build_llm_observations
from jo_pipeline.regroup import Regrouper, SessionBuilder, unassigned_paths
from jo_pipeline.reliability import ReliabilityReport
from jo_pipeline.review import GroupingReviewer, ReviewComparison
from llm_pipeline.client import PRICE_INPUT_USD_PER_MTOK, PRICE_OUTPUT_USD_PER_MTOK, OpenRouterClient
from llm_pipeline.prompts import PROMPT_VERSION as LLM_PROMPT_VERSION
from llm_pipeline.review import ReviewStore, ReviewSummary, parse_reviews, prompts_for, render_session, review_model_for, review_records_for, reviewer_for, schema_version_for
from llm_pipeline.schema import SCHEMA_VERSION as LLM_SCHEMA_VERSION
from llm_pipeline.store import RunStore, run_name
from llm_pipeline.styles import MOMENTS, STYLES, TOPIC_REVIEW, GroupingStyle, style_for

LOGGER = logging.getLogger(__name__)

REFERENCE_PATTERN = "**/*.docx"
COLLAPSED_MEMBERSHIPS = (DUPLICATE, BURST)
DEFAULT_LLM_VERSION = run_name(LLM_PROMPT_VERSION, LLM_SCHEMA_VERSION)
REVIEW_CHARS_PER_TOKEN = 4
REVIEW_SCHEMA_TOKENS = 250
REVIEW_OUTPUT_TOKENS = 400


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
    regrouped: list
    style: GroupingStyle
    unassigned: list
    image_signals: dict
    review_signals: dict
    sessions: list
    review_records: list
    model_calls: list
    llm_observations: dict
    model_id: str | None
    llm_method_version: str | None


@dataclass(frozen=True)
class ReviewInputs:
    manifest: DatasetManifest
    baseline: list
    signals_by_path: dict
    image_signals: dict
    style: GroupingStyle
    sessions: list


def build_sessions(dataset_id: str, llm_version: str, style: GroupingStyle, baseline: list, signals_by_path: dict, image_signals: dict) -> list:
    builder = SessionBuilder.for_style(style)
    return builder.build(dataset_id, llm_version, prompts_for(style).version, schema_version_for(style), baseline, signals_by_path, image_signals)


def load_llm_records(config: PipelineConfig, dataset_id: str, llm_version: str) -> list[dict]:
    records = RunStore(config.llm_runs_dir / dataset_id / llm_version).records()
    LOGGER.info(f"{dataset_id}: {len(records)} evaluation records read from the {llm_version} run directory")
    return records


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


def build_review_calls(records: list[dict]) -> list[ModelCall]:
    return [ModelCall(
        relative_path=None,
        provider=OPENROUTER_PROVIDER,
        model_id=record["model"],
        prompt_version=record["review_prompt_version"],
        schema_version=record["review_schema_version"],
        attempt=record["attempts"],
        validation_status=record["validation_status"],
        latency_ms=record["latency_ms"],
        prompt_tokens=record["prompt_tokens"],
        completion_tokens=record["completion_tokens"],
        cost_usd=record["cost_usd"],
        parsed_output=record["review"],
        failure_detail=record["failure_detail"],
        called_utc=record["reviewed_utc"],
    ) for record in records]


def build_llm_observation_rows(evaluations: dict, model_id: str, method_version: str) -> dict:
    rows = {}
    for relative_path, evaluation in evaluations.items():
        if evaluation:
            rows[relative_path] = build_llm_observations(evaluation, model_id, method_version)
        else:
            LOGGER.info(f"{relative_path}: evaluation failed validation, no observations to store")
    return rows


def build_run_artifacts(config: PipelineConfig, args: argparse.Namespace, manifest: DatasetManifest) -> RunArtifacts:
    assets = AssetLoader(config.dataset_path(args.dataset)).load(manifest)
    extracted = [asset for asset in assets if not asset.failure]
    signals = [build_signals(asset) for asset in extracted]
    signals_by_path = {signal.relative_path: signal for signal in signals}
    baseline = MomentGrouper().group(signals)

    records = load_llm_records(config, args.dataset, args.llm_version)
    evaluations = fan_out_evaluations(manifest, records)
    if records:
        model_id = records[0]["model"]
        schema_version = records[0]["schema_version"]
        llm_method_version = f"p{records[0]['prompt_version']}/{schema_version}"
        image_signals = build_image_signals(schema_version, evaluations)
    else:
        model_id = None
        schema_version = None
        llm_method_version = None
        image_signals = {}
    refined = ProposalRefiner().refine(baseline, image_signals)

    style = style_for(args.style)
    sessions = build_sessions(args.dataset, args.llm_version, style, baseline, signals_by_path, image_signals)
    review_records = review_records_for(ReviewStore.for_style(config.llm_runs_dir, args.dataset, style), sessions) if sessions else []
    reviews = parse_reviews(review_records, review_model_for(style))
    regrouper = Regrouper()
    regrouped = regrouper.regroup(baseline, signals_by_path, image_signals, sessions, reviews, style)
    review_signals = regrouper.review_signals(image_signals, sessions, reviews, style)
    unassigned = unassigned_paths(sessions, reviews) if style.kind == TOPIC_REVIEW else []

    if schema_version == SCHEMA_V3:
        llm_observations = build_llm_observation_rows(evaluations, model_id, llm_method_version)
    else:
        LOGGER.info(f"{args.dataset}: fetched observation rows are only flattened for {SCHEMA_V3} records, none built from {schema_version}")
        llm_observations = {}

    return RunArtifacts(
        manifest=manifest,
        extracted=extracted,
        baseline=baseline,
        refined=refined,
        regrouped=regrouped,
        style=style,
        unassigned=unassigned,
        image_signals=image_signals,
        review_signals=review_signals,
        sessions=sessions,
        review_records=review_records,
        model_calls=build_model_calls(records),
        llm_observations=llm_observations,
        model_id=model_id,
        llm_method_version=llm_method_version,
    )


def build_review_inputs(config: PipelineConfig, args: argparse.Namespace) -> ReviewInputs:
    manifest = ensure_manifest(config, args)
    assets = AssetLoader(config.dataset_path(args.dataset)).load(manifest)
    signals = [build_signals(asset) for asset in assets if not asset.failure]
    signals_by_path = {signal.relative_path: signal for signal in signals}
    baseline = MomentGrouper().group(signals)

    records = load_llm_records(config, args.dataset, args.llm_version)
    if not records:
        raise SystemExit(f"no evaluation records under {args.llm_version} for {args.dataset}, run llm_pipeline first or pass --llm-version")
    image_signals = build_image_signals(records[0]["schema_version"], fan_out_evaluations(manifest, records))
    style = style_for(args.style)
    sessions = build_sessions(args.dataset, args.llm_version, style, baseline, signals_by_path, image_signals)
    return ReviewInputs(manifest=manifest, baseline=baseline, signals_by_path=signals_by_path, image_signals=image_signals, style=style, sessions=sessions)


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
        if artifacts.review_records:
            store.save_model_calls(run_id, asset_ids, build_review_calls(artifacts.review_records))
            store.save_proposals(run_id, manifest.dataset_id, asset_ids, artifacts.regrouped)
        else:
            LOGGER.info(f"{manifest.dataset_id}: no moment review records, regrouped proposals not stored")
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
    proposals = len(artifacts.baseline) + (len(artifacts.refined) if artifacts.model_calls else 0) + (len(artifacts.regrouped) if artifacts.review_records else 0)
    calls = len(artifacts.model_calls) + len(artifacts.review_records)
    print(f"\n{args.dataset}: run {run_id} stored {len(artifacts.manifest.entries)} assets, {proposals} proposals and {calls} model calls in {config.database_path}\n")


def run_review_moments(config: PipelineConfig, args: argparse.Namespace):
    inputs = build_review_inputs(config, args)
    store = ReviewStore.for_style(config.llm_runs_dir, args.dataset, inputs.style)
    print_review_estimate(args.dataset, inputs.sessions, store, config.openrouter_model, inputs.style)
    if not config.openrouter_api_key:
        raise SystemExit("JO_OPENROUTER_API_KEY is not set, add it to .env before reviewing moments")

    with OpenRouterClient(config.openrouter_api_key, config.openrouter_model) as client:
        run = reviewer_for(client, store, args.dataset, inputs.style).review(inputs.sessions, limit=args.limit)
    print_review_run(run.summary, store.run_dir)
    print_style_groups(inputs, store)


def print_style_groups(inputs: ReviewInputs, store: ReviewStore):
    reviews = parse_reviews(review_records_for(store, inputs.sessions), review_model_for(inputs.style))
    regrouper = Regrouper()
    groups = regrouper.regroup(inputs.baseline, inputs.signals_by_path, inputs.image_signals, inputs.sessions, reviews, inputs.style)
    print(f"{inputs.style.name}: {len(groups)} groups from {len(inputs.sessions)} sessions\n")
    for index, proposal in enumerate(groups, start=1):
        review = proposal.evidence.get("review", {})
        title = review.get("title") or proposal.label
        why = f"  why: {', '.join(review['why'])}" if review.get("why") else ""
        print(f"  {index:>3}. {title:<44} {len(proposal.members):>3} photos  {proposal.label}{why}")
        if review.get("about"):
            print(f"       {review['about']}")
        elif review.get("reason"):
            print(f"       {review['reason']}")
    if inputs.style.kind == TOPIC_REVIEW:
        unassigned = unassigned_paths(inputs.sessions, reviews)
        print(f"\n  {len(unassigned)} photos not in this view\n")


def print_review_estimate(dataset_id: str, sessions: list, store: ReviewStore, model: str, style: GroupingStyle):
    system_tokens = len(prompts_for(style).system) // REVIEW_CHARS_PER_TOKEN + REVIEW_SCHEMA_TOKENS
    pending = [session for session in sessions if not store.exists(session.session_id)]
    print(f"\n{style.name} review for {dataset_id} with {model}\n")
    total = 0.0
    for session in pending:
        input_tokens = system_tokens + len(render_session(session)) // REVIEW_CHARS_PER_TOKEN
        cost = (input_tokens * PRICE_INPUT_USD_PER_MTOK + REVIEW_OUTPUT_TOKENS * PRICE_OUTPUT_USD_PER_MTOK) / 1_000_000
        total += cost
        print(f"  {session.session_id}  {len(session.frames):>3} photos  {session.frames[0].captured_utc[:16]} to {session.frames[-1].captured_utc[11:16]}  ~{input_tokens} tokens in  ~${cost:.4f}")
    print(f"\n{len(pending)} sessions pending of {len(sessions)}, {len(sessions) - len(pending)} already reviewed")
    print(f"Estimated total: ~${total:.2f} at ${PRICE_INPUT_USD_PER_MTOK}/M in, ${PRICE_OUTPUT_USD_PER_MTOK}/M out, text only, no images sent\n")


def print_review_run(summary: ReviewSummary, run_dir: Path):
    print(f"\n{summary.dataset_id}: {summary.style} reviewed with {summary.model}, review prompt {summary.review_prompt_version}, schema {summary.review_schema_version}\n")
    print(f"  sessions           {summary.sessions_total}")
    print(f"  skipped existing   {summary.sessions_skipped_existing}")
    print(f"  reviewed           {summary.sessions_reviewed}")
    print(f"  valid              {summary.valid}")
    print(f"  invalid            {summary.invalid}")
    print(f"  failed requests    {summary.request_failed}")
    print(f"  tokens             {summary.total_prompt_tokens} in, {summary.total_completion_tokens} out")
    print(f"  cost               ${summary.total_cost_usd:.4f}")
    print(f"  records            {run_dir}\n")


def run_review(config: PipelineConfig, args: argparse.Namespace):
    signals = [build_signals(asset) for asset in load_assets(config, args) if not asset.failure]
    proposals = MomentGrouper().group(signals)
    document = find_reference_document(config.dataset_path(args.dataset), args.reference)
    if not document:
        raise FileNotFoundError(f"no reference document found under {config.dataset_path(args.dataset)}, pass --reference with a path")

    reference = reference_reader(document).read(args.dataset, signals)
    comparison = GroupingReviewer().compare(proposals, reference)
    artifact = write_review_artifact(config, args, proposals, reference, comparison)
    print_review(args.dataset, reference, comparison, artifact)


def run_benchmark(config: PipelineConfig, args: argparse.Namespace):
    manifest = ensure_manifest(config, args)
    artifacts = build_run_artifacts(config, args, manifest)
    signals = [build_signals(asset) for asset in artifacts.extracted]
    image_signals = artifacts.image_signals

    print(f"\n{'=' * 78}\n{args.dataset}  benchmark  model {artifacts.model_id or 'none'}  records {args.llm_version}  style {artifacts.style.id}\n{'=' * 78}")
    print(f"\n{len(image_signals)} of {len(signals)} extracted images carry a valid evaluation\n")
    print_refinement_effect(artifacts)
    print_review_effect(artifacts)

    document = find_reference_document(config.dataset_path(args.dataset), args.reference)
    if not document:
        print("Reference: no reference grouping supplied for this dataset, accuracy not scored\n")
        return

    reference = reference_reader(document).read(args.dataset, signals)
    comparisons = [GroupingReviewer().compare(proposals, reference) for proposals in (artifacts.baseline, artifacts.refined, artifacts.regrouped)]
    keep = KeepSignalScorer().score(reference, image_signals)
    review_keep = KeepSignalScorer().score(reference, artifacts.review_signals)
    print_benchmark(comparisons, keep, review_keep)

    artifact = write_benchmark_artifact(config, args, artifacts, comparisons, keep, review_keep)
    print(f"  artifact                   {artifact}\n")


def print_review_effect(artifacts: RunArtifacts):
    reviewed = [proposal for proposal in artifacts.regrouped if proposal.evidence.get("review", {}).get("status") == "reviewed"]
    changes = {}
    for proposal in reviewed:
        change = proposal.evidence["review"].get("change", "grouped")
        changes[change] = changes.get(change, 0) + 1
    valid = sum(1 for record in artifacts.review_records if record["validation_status"] == "valid")
    cost = sum(record["cost_usd"] for record in artifacts.review_records)
    left_out = sum(1 for proposal in artifacts.regrouped for entry in proposal.evidence.get("excluded_by_signal", []) if entry.get("source") == "moment_review")
    print(f"{artifacts.style.name} review: {len(artifacts.sessions)} sessions, {valid} valid reviews of {len(artifacts.review_records)} records, ${cost:.4f}\n")
    print(f"  regrouped proposals           {len(artifacts.regrouped)} from {len(artifacts.baseline)} baseline")
    print(f"  moments reviewed              {len(reviewed)}  " + ", ".join(f"{count} {change}" for change, count in sorted(changes.items())))
    print(f"  photos left out by the review {left_out}")
    print(f"  photos not in this view       {len(artifacts.unassigned)}\n")


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


def print_benchmark(comparisons: list[ReviewComparison], keep: KeepSignalScore, review_keep: KeepSignalScore):
    baseline, refined, regrouped = comparisons
    print(f"Accuracy against the traveler's own grouping, baseline against refined against regrouped\n")
    print(f"  {'metric':<26} {'baseline':>9} {'refined':>9} {'regrouped':>9}  delta")
    for label, left, middle, right in (
        ("pair precision", baseline.pair_precision, refined.pair_precision, regrouped.pair_precision),
        ("pair recall", baseline.pair_recall, refined.pair_recall, regrouped.pair_recall),
        ("pair f1", baseline.pair_f1, refined.pair_f1, regrouped.pair_f1),
    ):
        print(f"  {label:<26} {left:>9.3f} {middle:>9.3f} {right:>9.3f}  {right - left:+.3f}")
    for label, left, middle, right in (
        ("proposals", baseline.proposed_groups, refined.proposed_groups, regrouped.proposed_groups),
        ("reference memories split", baseline.split_groups, refined.split_groups, regrouped.split_groups),
        ("proposals merging memories", baseline.merged_proposals, refined.merged_proposals, regrouped.merged_proposals),
        ("excluded photos grouped", baseline.excluded_assets_grouped, refined.excluded_assets_grouped, regrouped.excluded_assets_grouped),
    ):
        print(f"  {label:<26} {left:>9} {middle:>9} {right:>9}  {right - left:+d}")

    print_keep_score("per-image keep signal", keep)
    print_keep_score("keep signal with the moment review", review_keep)


def print_keep_score(title: str, keep: KeepSignalScore):
    print(f"\n{title[0].upper()}{title[1:]} against the {keep.excluded_total} photos the traveler left out\n")
    print(f"  evaluated exclusions       {keep.excluded_with_signal} of {keep.excluded_total}")
    print(f"  caught as leave_out        {keep.excluded_caught}")
    print(f"  recall                     {keep.recall:.2f}")
    print(f"  false positives            {keep.grouped_false_positives} of {keep.grouped_with_signal} kept photos")
    print(f"  false positive rate        {keep.false_positive_rate:.2f}")
    for hit in keep.missed:
        print(f"  missed  {hit.relative_path}  said {hit.keep_signal} at {hit.confidence}")
    for hit in keep.false_positives:
        print(f"  false   {hit.relative_path}  {hit.reason}")


def write_benchmark_artifact(config: PipelineConfig, args: argparse.Namespace, artifacts: RunArtifacts, comparisons: list[ReviewComparison], keep: KeepSignalScore, review_keep: KeepSignalScore) -> Path:
    baseline, refined, regrouped = comparisons
    config.runs_dir.mkdir(parents=True, exist_ok=True)
    artifact = config.runs_dir / f"{args.dataset}-v{args.dataset_version}-benchmark.json"
    artifact.write_text(json.dumps({
        "dataset_id": args.dataset,
        "dataset_version": args.dataset_version,
        "model_id": artifacts.model_id,
        "llm_method_version": artifacts.llm_method_version,
        "baseline_comparison": baseline.as_dict(),
        "refined_comparison": refined.as_dict(),
        "regrouped_comparison": regrouped.as_dict(),
        "keep_signal_score": keep.as_dict(),
        "review_keep_signal_score": review_keep.as_dict(),
        "review": {
            "style": artifacts.style.id,
            "sessions": len(artifacts.sessions),
            "records": artifacts.review_records,
            "unassigned": artifacts.unassigned,
            "cost_usd": round(sum(record["cost_usd"] for record in artifacts.review_records), 4),
        },
        "baseline_proposals": [asdict(proposal) for proposal in artifacts.baseline],
        "refined_proposals": [asdict(proposal) for proposal in artifacts.refined],
        "regrouped_proposals": [asdict(proposal) for proposal in artifacts.regrouped],
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

    reference = reference_reader(document).read(args.dataset, signals)
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
    parser.add_argument("--llm-version", default=DEFAULT_LLM_VERSION, help=f"Evaluation run directory under data/llm_runs/<dataset>, such as p1-llm-eval-1 (default {DEFAULT_LLM_VERSION})")
    parser.add_argument("--style", default=MOMENTS, choices=list(STYLES), help=f"Grouping style put to the model (default {MOMENTS})")


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

    benchmark = subparsers.add_parser("benchmark", help="Score the deterministic baseline against the visual model refinement and the moment review, no API calls")
    add_dataset_arguments(benchmark)
    benchmark.add_argument("--reference", help="Path to the reference document, discovered under the dataset folder when omitted")
    benchmark.set_defaults(handler=run_benchmark)

    review_moments = subparsers.add_parser("review-moments", help="Send each outing's per-image descriptions to the model as text and store its moment review, the only paid action here")
    add_dataset_arguments(review_moments)
    review_moments.add_argument("--limit", type=int, help="Maximum number of pending sessions to review this run")
    review_moments.set_defaults(handler=run_review_moments)

    return parser


def main():
    args = build_parser().parse_args()
    config = load_config()
    configure_logging(config.log_level)
    LOGGER.debug(f"command {args.command} starting against dataset root {config.dataset_root}")
    args.handler(config, args)
