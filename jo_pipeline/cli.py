import argparse
import logging

from jo_pipeline.config import PipelineConfig, load_config
from jo_pipeline.logging_setup import configure_logging
from jo_pipeline.manifest import InventoryScanner

LOGGER = logging.getLogger(__name__)


def run_inventory(config: PipelineConfig, args: argparse.Namespace):
    scanner = InventoryScanner(config.dataset_root, config.manifest_dir)
    manifest = scanner.scan(args.dataset, args.dataset_version)
    scanner.write(manifest)
    for entry in manifest.entries:
        LOGGER.info(f"{args.dataset}: {entry.relative_path} {entry.media_type} {entry.size_bytes} bytes {entry.sha256[:12]}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="jo-pipeline", description="Journey Onward media metadata and grouping pipeline")
    subparsers = parser.add_subparsers(dest="command", required=True)

    inventory = subparsers.add_parser("inventory", help="Scan a dataset folder and write a versioned manifest")
    inventory.add_argument("--dataset", required=True, help="Dataset folder name under the dataset root")
    inventory.add_argument("--dataset-version", default="1", help="Manifest version to write")
    inventory.set_defaults(handler=run_inventory)

    return parser


def main():
    args = build_parser().parse_args()
    config = load_config()
    configure_logging(config.log_level)
    LOGGER.debug(f"command {args.command} starting against dataset root {config.dataset_root}")
    args.handler(config, args)
