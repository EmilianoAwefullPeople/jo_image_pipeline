import argparse
import logging
from pathlib import Path

from llm_pipeline.client import OpenRouterClient, PRICE_INPUT_USD_PER_MTOK, PRICE_OUTPUT_USD_PER_MTOK
from llm_pipeline.config import LlmConfig, load_config
from llm_pipeline.discovery import discover_images
from llm_pipeline.logging_setup import configure_logging
from llm_pipeline.prompts import PROMPT_VERSION, default_prompts, load_system_prompt
from llm_pipeline.runner import EvaluationRunner, pending_images
from llm_pipeline.schema import SCHEMA_VERSION
from llm_pipeline.store import RunStore, RunSummary

LOGGER = logging.getLogger(__name__)

ESTIMATED_IMAGE_TOKENS = 800
ESTIMATED_OUTPUT_TOKENS = 1500
CHARS_PER_TOKEN = 4


def build_store(config: LlmConfig, dataset_id: str) -> RunStore:
    return RunStore(config.llm_runs_dir, dataset_id, PROMPT_VERSION, SCHEMA_VERSION)


def run_evaluation(config: LlmConfig, args: argparse.Namespace):
    if not config.openrouter_api_key:
        raise SystemExit("JO_OPENROUTER_API_KEY is not set, add it to .env before running evaluations")

    dataset_path = config.dataset_path(args.dataset)
    store = build_store(config, args.dataset)
    images = discover_images(dataset_path)

    with OpenRouterClient(config.openrouter_api_key, config.openrouter_model) as client:
        evaluation = EvaluationRunner(client, store, args.dataset, dataset_path, default_prompts()).run(images, limit=args.limit)

    print_run_summary(evaluation.summary, store.run_dir)


def run_estimate(config: LlmConfig, args: argparse.Namespace):
    dataset_path = config.dataset_path(args.dataset)
    store = build_store(config, args.dataset)
    images = discover_images(dataset_path)
    pending = pending_images(store, images)
    if args.limit is not None:
        pending = pending[: args.limit]

    system_tokens = len(load_system_prompt()) // CHARS_PER_TOKEN
    input_tokens = ESTIMATED_IMAGE_TOKENS + system_tokens
    per_image_cost = (input_tokens * PRICE_INPUT_USD_PER_MTOK + ESTIMATED_OUTPUT_TOKENS * PRICE_OUTPUT_USD_PER_MTOK) / 1_000_000

    print(f"\nEstimate for {args.dataset} with {config.openrouter_model} (no API calls made)\n")
    for image in pending:
        print(f"  {image.relative_path:<50} {image.size_bytes:>12,} bytes  ~${per_image_cost:.4f}")
    print(f"\n{len(pending)} images pending, {len(images) - len(pending)} already evaluated or excluded by limit")
    print(f"Assumes ~{input_tokens} input and ~{ESTIMATED_OUTPUT_TOKENS} output tokens per image at ${PRICE_INPUT_USD_PER_MTOK}/M in, ${PRICE_OUTPUT_USD_PER_MTOK}/M out")
    print(f"Estimated total: ~${per_image_cost * len(pending):.2f}\n")


def print_run_summary(summary: RunSummary, run_dir: Path):
    print(f"\n{summary.dataset_id}: evaluated with {summary.model}, prompt v{summary.prompt_version}, schema {summary.schema_version}\n")
    print(f"  discovered         {summary.images_discovered}")
    print(f"  skipped existing   {summary.images_skipped_existing}")
    print(f"  evaluated          {summary.images_evaluated}")
    print(f"  valid              {summary.valid}")
    print(f"  invalid            {summary.invalid}")
    print(f"  failed requests    {summary.request_failed}")
    print(f"  tokens             {summary.total_prompt_tokens} in, {summary.total_completion_tokens} out")
    print(f"  cost               ${summary.total_cost_usd:.4f}")
    print(f"  records            {run_dir}\n")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="llm-pipeline", description="Journey Onward visual LLM image evaluation via OpenRouter")
    subparsers = parser.add_subparsers(dest="command", required=True)

    run = subparsers.add_parser("run", help="Evaluate dataset images through the visual model and store records")
    run.add_argument("--dataset", required=True, help="Dataset folder name under the dataset root")
    run.add_argument("--limit", type=int, help="Maximum number of pending images to evaluate this run")
    run.set_defaults(handler=run_evaluation)

    estimate = subparsers.add_parser("estimate", help="List pending images and estimated cost without calling the API")
    estimate.add_argument("--dataset", required=True, help="Dataset folder name under the dataset root")
    estimate.add_argument("--limit", type=int, help="Maximum number of pending images to include")
    estimate.set_defaults(handler=run_estimate)

    return parser


def main():
    args = build_parser().parse_args()
    config = load_config()
    configure_logging(config.log_level)
    LOGGER.debug(f"command {args.command} starting against dataset root {config.dataset_root}")
    args.handler(config, args)
