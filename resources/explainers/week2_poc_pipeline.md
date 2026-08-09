# Week 2 PoC Pipeline

Local, single-process Python pipeline that turns a supplied trip photo folder into traceable metadata and
provisional moment groups. No iPhone client, upload flow or hosted infrastructure is part of this build.

## Stages

The pipeline runs as eleven ordered stages. Each stage records enough evidence to reproduce its output and to
compare later prompt, model or heuristic changes.

| # | Stage | Responsibility |
|---|-------|----------------|
| 1 | Configure | Select a dataset and pin the run configuration: schema version, prompt version, model id |
| 2 | Inventory | Scan the dataset folder without modifying it and record path, media type, size and SHA-256 |
| 3 | Extract | Read EXIF, dimensions and file properties; derive deterministic OpenCV signals |
| 4 | Normalize | Map raw values into the canonical metadata schema, preserving original fields and missing-value evidence |
| 5 | Enrich | Resolve place, timezone candidate and historical weather from GPS and capture time |
| 6 | Infer | Send an approved image derivative plus reliable context to the visual model under a strict JSON schema |
| 7 | Validate | Validate the model response, retry once on failure, then record the second failure without further recovery |
| 8 | Persist | Store observations, provenance, model and prompt versions, raw and parsed output, latency, tokens and cost |
| 9 | Group | Apply time, location, burst and duplicate grouping, with visual labels as supporting evidence only |
| 10 | Review | Compare proposals against the reference grouping and record accept, split, merge, reject or correct |
| 11 | Benchmark | Promote strong cases and failures into a versioned regression set |

## Signal trust rules

These rules constrain every stage and are the reason the schema separates values rather than overwriting them.

- Retain the raw source value alongside the normalized value, the source mechanism, the processing version and
  the selection reason.
- Machine-derived values stay separate from traveler-confirmed values. A correction creates a new confirmed
  record; it never rewrites source history.
- Missing values are a valid outcome. The pipeline does not invent coordinates, timezone, landmark or identity.
- Confidence is supporting metadata, not proof. Store the evidence and the model, prompt or algorithm version
  that produced it.
- Visual model output explains or resolves ambiguous boundaries. It is never the sole grouping authority.

## Datasets and manifests

Supplied datasets live in `resources/images/<dataset_id>/` and are treated as read-only originals. Before a run,
Inventory writes a versioned manifest to `data/manifests/<dataset_id>-v<version>.json` containing every file
reference, media type, size, modification time and content hash.

A manifest version is immutable. Rescanning an existing version fails rather than overwriting, so a processing
run can always be traced back to the exact file set it consumed. Hidden files are skipped; unrecognised
extensions are recorded as `unknown` rather than dropped, so reference documents supplied alongside the photos
stay visible in the record.

The Shanghai dataset ships a human grouping document under `Grouped & Unpacked/`. That grouping is a comparison
point for the Review stage, not absolute ground truth.

## Storage

SQLite is the PoC store, created by `scripts/init_db.py` from `schema/001_initial.sql`. Application code never
creates or migrates the database. Column types stay close to the PostgreSQL target described in the Week 1
architecture comparison so the same table shape can be promoted without redesign.

Key tables: `datasets` and `media_assets` for the file set, `processing_runs` for run provenance,
`metadata_observations` for one value per field and source, `model_calls` for cost, latency, tokens and
validation status, `group_proposals` with `group_members` for machine grouping, and `review_decisions` for
reviewer actions kept separate from the proposals they judge.

## Dependencies

Pillow with pillow-heif decodes HEIC and reads EXIF. OpenCV and NumPy provide deterministic image signals.
timezonefinder derives a timezone candidate when EXIF lacks a reliable offset. httpx calls OpenRouter and the
enrichment services. Pydantic enforces the model response schema. ExifTool is an optional diagnostic fallback
for tags Pillow does not expose and is not required for the baseline run.
