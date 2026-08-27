import argparse
import json
import logging
import statistics
from pathlib import Path

from jo_pipeline.config import load_config
from jo_pipeline.logging_setup import configure_logging
from llm_pipeline.prompts import PROMPT_VERSION
from llm_pipeline.schema import SCHEMA_VERSION
from llm_pipeline.store import run_name

LOGGER = logging.getLogger(__name__)

FIELD_CHECKS = (
    ("1. general_description", lambda evaluation: bool(evaluation["general_description"])),
    ("2. scene_setting", lambda evaluation: bool(evaluation["scene_setting"]["types"])),
    ("3. landmark", lambda evaluation: evaluation["landmark"]["name"] is not None),
    ("4. notable_subjects", lambda evaluation: bool(evaluation["notable_subjects"])),
    ("5. focal_points", lambda evaluation: bool(evaluation["focal_points"])),
    ("6. activity", lambda evaluation: bool(evaluation["activity"]["types"])),
    ("7. environment", lambda evaluation: bool(evaluation["environment"]["types"])),
    ("8. composition", lambda evaluation: bool(evaluation["composition"])),
    ("9. weather", lambda evaluation: bool(evaluation["weather"])),
    ("10. keyword_tags", lambda evaluation: bool(evaluation["keyword_tags"])),
    ("11. photographic_style", lambda evaluation: bool(evaluation["photographic_style"]["types"])),
    ("extra: why_tags", lambda evaluation: bool(evaluation["why_tags"])),
)


def record_files(run_dir: Path) -> list[Path]:
    return [path for path in sorted(run_dir.glob("*.json")) if not path.name.startswith("run-")]


def percentile(values: list[int], fraction: float) -> int:
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(len(ordered) * fraction))]


def summarise_evaluations(records: list[dict]) -> dict:
    latencies = [record["latency_ms"] for record in records]
    populated = {}
    valid = [record for record in records if record["validation_status"] == "valid"]
    for label, check in FIELD_CHECKS:
        count = sum(1 for record in valid if check(record["evaluation"]))
        populated[label] = {"populated": count, "of": len(valid), "rate": round(count / len(valid), 3) if valid else 0.0}

    return {
        "records": len(records),
        "valid": len(valid),
        "invalid": len(records) - len(valid),
        "retried": sum(1 for record in records if record["attempts"] > 1),
        "prompt_tokens": sum(record["prompt_tokens"] for record in records),
        "completion_tokens": sum(record["completion_tokens"] for record in records),
        "cost_usd": round(sum(record["cost_usd"] for record in records), 4),
        "latency_ms_median": int(statistics.median(latencies)) if latencies else 0,
        "latency_ms_p95": percentile(latencies, 0.95) if latencies else 0,
        "field_population": populated,
    }


def summarise_reviews(dataset_dir: Path) -> dict:
    summaries = {}
    for review_dir in sorted(dataset_dir.glob("review-*")):
        records = [json.loads(path.read_text()) for path in record_files(review_dir)]
        summaries[review_dir.name] = {
            "records": len(records),
            "valid": sum(1 for record in records if record["validation_status"] == "valid"),
            "cost_usd": round(sum(record["cost_usd"] for record in records), 4),
            "latency_ms_median": int(statistics.median([record["latency_ms"] for record in records])) if records else 0,
        }
    return summaries


def main():
    parser = argparse.ArgumentParser(prog="eval-set-report")
    parser.add_argument("--dataset", required=True, help="Dataset folder name under the dataset root")
    parser.add_argument("--llm-version", default=run_name(PROMPT_VERSION, SCHEMA_VERSION), help="Evaluation run directory under data/llm_runs/<dataset>")
    args = parser.parse_args()

    configure_logging("INFO")
    config = load_config()
    run_dir = config.llm_runs_dir / args.dataset / args.llm_version
    records = [json.loads(path.read_text()) for path in record_files(run_dir)]
    if not records:
        raise SystemExit(f"no evaluation records under {run_dir}")

    report = {
        "dataset_id": args.dataset,
        "llm_version": args.llm_version,
        "model": records[0]["model"],
        "evaluation": summarise_evaluations(records),
        "reviews": summarise_reviews(config.llm_runs_dir / args.dataset),
    }
    artifact = config.runs_dir / f"{args.dataset}-{args.llm_version}-results.json"
    artifact.parent.mkdir(parents=True, exist_ok=True)
    artifact.write_text(json.dumps(report, indent=2) + "\n")

    evaluation = report["evaluation"]
    print(f"\n{args.dataset}  {args.llm_version}  {report['model']}")
    print(f"  records {evaluation['records']}, valid {evaluation['valid']}, invalid {evaluation['invalid']}, retried {evaluation['retried']}")
    print(f"  tokens {evaluation['prompt_tokens']} in / {evaluation['completion_tokens']} out, cost ${evaluation['cost_usd']:.4f}")
    print(f"  latency median {evaluation['latency_ms_median']} ms, p95 {evaluation['latency_ms_p95']} ms")
    for label, row in evaluation["field_population"].items():
        print(f"  {label:<26} {row['populated']}/{row['of']}  {row['rate']:.0%}")
    for name, row in report["reviews"].items():
        print(f"  {name:<46} {row['records']} records, {row['valid']} valid, ${row['cost_usd']:.4f}, median {row['latency_ms_median']} ms")
    print(f"  artifact {artifact}\n")


if __name__ == "__main__":
    main()
