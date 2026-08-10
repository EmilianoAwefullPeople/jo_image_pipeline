import argparse
import json
import logging
from dataclasses import asdict
from pathlib import Path

from jo_pipeline.assets import AssetLoader, build_signals
from jo_pipeline.config import PipelineConfig, load_config, resolve_path
from jo_pipeline.group import MomentGrouper
from jo_pipeline.logging_setup import configure_logging
from jo_pipeline.manifest import InventoryScanner
from jo_pipeline.reference import ReferenceGrouping, ReferenceReader
from jo_pipeline.reliability import ReliabilityReport
from jo_pipeline.review import GroupingReviewer, ReviewComparison

LOGGER = logging.getLogger(__name__)

REFERENCE_PATTERN = "**/*.docx"


def load_assets(config: PipelineConfig, args: argparse.Namespace) -> list:
    scanner = InventoryScanner(config.dataset_root, config.manifest_dir)
    manifest = scanner.load(args.dataset, args.dataset_version)
    return AssetLoader(config.dataset_path(args.dataset)).load(manifest)


def run_inventory(config: PipelineConfig, args: argparse.Namespace):
    scanner = InventoryScanner(config.dataset_root, config.manifest_dir)
    manifest = scanner.scan(args.dataset, args.dataset_version)
    scanner.write(manifest)
    for entry in manifest.entries:
        LOGGER.info(f"{args.dataset}: {entry.relative_path} {entry.media_type} {entry.size_bytes} bytes {entry.sha256[:12]}")


def run_extract(config: PipelineConfig, args: argparse.Namespace):
    report = ReliabilityReport()
    for asset in load_assets(config, args):
        if asset.failure:
            report.add_failure(asset.entry.relative_path, asset.failure)
        else:
            report.add_asset(asset.entry.relative_path, asset.observations)

    print_reliability(args.dataset, report)


def run_group(config: PipelineConfig, args: argparse.Namespace):
    signals = [build_signals(asset) for asset in load_assets(config, args) if not asset.failure]
    proposals = MomentGrouper().group(signals)
    print_groups(args.dataset, signals, proposals)


def run_review(config: PipelineConfig, args: argparse.Namespace):
    signals = [build_signals(asset) for asset in load_assets(config, args) if not asset.failure]
    proposals = MomentGrouper().group(signals)
    document = find_reference_document(config.dataset_path(args.dataset), args.reference)
    reference = ReferenceReader(document).read(args.dataset, signals)
    comparison = GroupingReviewer().compare(proposals, reference)
    artifact = write_review_artifact(config, args, proposals, reference, comparison)
    print_review(args.dataset, reference, comparison, artifact)


def find_reference_document(dataset_path: Path, override: str | None) -> Path:
    if override:
        return resolve_path(override)

    documents = sorted(dataset_path.glob(REFERENCE_PATTERN))
    if not documents:
        raise FileNotFoundError(f"no reference document found under {dataset_path}, pass --reference with a path")

    LOGGER.info(f"using reference document {documents[0]}")
    return documents[0]


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


def print_review(dataset_id: str, reference: ReferenceGrouping, comparison: ReviewComparison, artifact: Path):
    print(f"\n{dataset_id}: {comparison.reference_groups} reference groups against {comparison.proposed_groups} proposals\n")
    print(f"  assets compared          {comparison.compared_assets}")
    print(f"  pair precision           {comparison.pair_precision:.2f}")
    print(f"  pair recall              {comparison.pair_recall:.2f}")
    print(f"  pair f1                  {comparison.pair_f1:.2f}")
    print(f"  reference groups split   {comparison.split_groups}")
    print(f"  proposals merging groups {comparison.merged_proposals}")
    print(f"  excluded photos grouped  {comparison.excluded_assets_grouped} of {len(reference.excluded_paths)}")
    print(f"  reference images unmatched {len(reference.unmatched_media)}")
    print(f"\n  artifact {artifact}")


def print_reliability(dataset_id: str, report: ReliabilityReport):
    LOGGER.info(f"{dataset_id}: extracted {report.assets} images with {len(report.failures)} failures")
    print(f"\n{dataset_id}: {report.assets} images, {len(report.failures)} extraction failures\n")
    print(f"{'category':<10} {'field':<24} {'present':>9} {'rate':>7}  common unknown reason")
    for row in report.rows():
        leading_reason = max(row.unknown_reasons, key=row.unknown_reasons.get, default="")
        print(f"{row.category:<10} {row.field:<24} {row.present:>4}/{row.total:<4} {row.presence_rate:>6.0%}  {leading_reason}")

    for relative_path, detail in report.failures.items():
        print(f"failure  {relative_path}  {detail}")


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


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="jo-pipeline", description="Journey Onward media metadata and grouping pipeline")
    subparsers = parser.add_subparsers(dest="command", required=True)

    inventory = subparsers.add_parser("inventory", help="Scan a dataset folder and write a versioned manifest")
    inventory.add_argument("--dataset", required=True, help="Dataset folder name under the dataset root")
    inventory.add_argument("--dataset-version", default="1", help="Manifest version to write")
    inventory.set_defaults(handler=run_inventory)

    extract = subparsers.add_parser("extract", help="Extract and normalize metadata for a manifested dataset")
    extract.add_argument("--dataset", required=True, help="Dataset folder name under the dataset root")
    extract.add_argument("--dataset-version", default="1", help="Manifest version to read")
    extract.set_defaults(handler=run_extract)

    group = subparsers.add_parser("group", help="Propose moment groups for a manifested dataset")
    group.add_argument("--dataset", required=True, help="Dataset folder name under the dataset root")
    group.add_argument("--dataset-version", default="1", help="Manifest version to read")
    group.set_defaults(handler=run_group)

    review = subparsers.add_parser("review", help="Compare proposed groups against the supplied reference grouping")
    review.add_argument("--dataset", required=True, help="Dataset folder name under the dataset root")
    review.add_argument("--dataset-version", default="1", help="Manifest version to read")
    review.add_argument("--reference", help="Path to the reference document, discovered under the dataset folder when omitted")
    review.set_defaults(handler=run_review)

    return parser


def main():
    args = build_parser().parse_args()
    config = load_config()
    configure_logging(config.log_level)
    LOGGER.debug(f"command {args.command} starting against dataset root {config.dataset_root}")
    args.handler(config, args)
