import argparse
import json
import logging
from dataclasses import asdict
from pathlib import Path

from jo_pipeline.assets import AssetLoader, build_signals
from jo_pipeline.config import PipelineConfig, load_config, resolve_path
from jo_pipeline.group import BURST, DUPLICATE, MomentGrouper
from jo_pipeline.logging_setup import configure_logging
from jo_pipeline.manifest import DatasetManifest, InventoryScanner, manifest_digest
from jo_pipeline.persist import COMPLETE, PipelineStore
from jo_pipeline.reference import ReferenceGrouping, ReferenceReader
from jo_pipeline.reliability import ReliabilityReport
from jo_pipeline.review import GroupingReviewer, ReviewComparison

LOGGER = logging.getLogger(__name__)

REFERENCE_PATTERN = "**/*.docx"
COLLAPSED_MEMBERSHIPS = (DUPLICATE, BURST)


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


def persist_run(config: PipelineConfig, manifest: DatasetManifest, extracted: list, proposals: list) -> int:
    with PipelineStore(config.database_path) as store:
        store.save_dataset(manifest)
        asset_ids = store.save_assets(manifest)
        run_id = store.start_run(manifest, manifest_digest(manifest), None)
        store.save_observations(run_id, asset_ids, extracted)
        store.save_proposals(run_id, manifest.dataset_id, asset_ids, proposals)
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
    manifest = load_manifest(config, args)
    assets = AssetLoader(config.dataset_path(args.dataset)).load(manifest)
    extracted = [asset for asset in assets if not asset.failure]
    proposals = MomentGrouper().group([build_signals(asset) for asset in extracted])
    run_id = persist_run(config, manifest, extracted, proposals)
    print(f"\n{args.dataset}: run {run_id} stored {len(manifest.entries)} assets and {len(proposals)} proposals in {config.database_path}\n")


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

    run_id = persist_run(config, manifest, extracted, proposals)
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

    return parser


def main():
    args = build_parser().parse_args()
    config = load_config()
    configure_logging(config.log_level)
    LOGGER.debug(f"command {args.command} starting against dataset root {config.dataset_root}")
    args.handler(config, args)
