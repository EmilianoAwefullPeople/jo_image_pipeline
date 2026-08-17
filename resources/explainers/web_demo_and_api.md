# Web demo and API

`jo_web` exposes the pipeline over HTTP: a single page anyone can upload photos to, and the JSON API underneath it that is intended to become the basis of the production system. It adds no analysis of its own — it composes `jo_pipeline` and `llm_pipeline` and reports what they produce.

## Each run is its own dataset

The pipeline is built around `dataset_root / <dataset_id>/`. The web layer satisfies that by building a `PipelineConfig` and an `LlmConfig` **per run** with absolute paths, rather than mutating process-wide environment variables (which would be a data race in a threaded server):

```
data/web_runs/<run_id>/input/                          uploaded files, dataset id "input"
data/web_runs/<run_id>/manifests/input-v1.json
data/web_runs/<run_id>/llm_runs/input/p1-llm-eval-1/   one record per content hash
data/web_runs/<run_id>/thumbnails/<sha256[:16]>.jpg
```

Everything a run produces nests under one directory, so retiring a run is a single `rmtree`. Nothing is written inside `input/`, so the recursive scans in `InventoryScanner` and `discover_images` never pick up the pipeline's own artifacts. `run_id` is a uuid4 hex, generated server side and never taken from the request.

## Stages

`created` → `queued` → `inventorying` → `extracting` → `thumbnailing` → `grouping` → `evaluating` → `refining` → `complete`, or `failed` with a `failure_detail`. The client polls `GET /api/runs/{id}` for the current stage and results.

The run payload carries both `groups` and `baseline_groups`. `baseline_groups` is what capture time, GPS and OpenCV alone produced; `groups` is that result after the visual model re-elected representatives on composition rather than sharpness, dropped anything it read as not worth keeping, and flagged screenshots without removing them. Showing both is the point — it is the only way a viewer can see what the model contributed rather than taking it on trust. When no API key is configured the two are identical and the demo still works, returning metadata and grouping only.

Photos the model drops are listed in the proposal's `evidence.excluded_by_signal` with the model's reason and confidence, and rendered under their moment rather than hidden, so a viewer can disagree with the call.

Thumbnails are written during the run rather than transcoded per request. Most customer media is HEIC, which no browser renders, and a full HEIC decode peaks near 250 MB — a gallery of them loading in parallel would exhaust the container. Serving pre-built JPEGs keyed by content hash also means the thumbnail route never builds a path from client input.

## One run at a time

A single worker thread drains a FIFO queue. This is a memory decision: extraction peaks around 330 MB resident on 12 MP HEIC files, because `sample_array` converts to RGB at full resolution before thumbnailing. On a 1 GB instance that admits one concurrent run. Extraction is fast (roughly 7 s for 50 images), so serialising costs little; a waiting run reports its queue position.

Within a run, the visual evaluation stage does run concurrently — it is network bound, not CPU bound, and a serial loop at 10-30 s per image would take 8-25 minutes for a 50 image set.

`timezonefinder` instances must not be shared across threads. One `AssetLoader` per run creates one `MetadataNormalizer` and one lazily constructed finder, which keeps this safe. Do not cache either object across runs to save its initialisation cost.

## Upload rules

Uploads arrive one file per request, which avoids request body and timeout limits and gives the browser real per-file progress. Each file must clear:

- an extension in the accepted set (HEIC, HEIF, JPEG, PNG, TIFF)
- the per-file, per-run and per-run-count caps
- a pixel ceiling, checked from the image header, which also rejects decompression bombs before they reach a stage that would treat them as a crash rather than a per-asset failure

Filenames are reduced to a bare name and rejected if empty, dotted or hidden; a repeated name is given the next free index rather than overwriting its predecessor. Anything refused is reported back and listed in the run's `skipped` array, because a file that silently disappears reads as lost data.

## Duplicates

Byte-identical files are kept on disk and both reach grouping, where `DuplicateIndex` uses the shared hash as a first-class signal and collapses the later copy. Only the **evaluation** list is deduplicated by hash, so an identical pair is described once and paid for once, and the single record is shared by every path with that hash.

## Cost and exposure

The deployed demo has no authentication and evaluation is always on, so anyone with the URL can spend OpenRouter credit. The caps above are the only brake. Cost, token counts and per-image failures are reported in every run payload so spend is visible rather than inferred.

Records are keyed by content hash, so re-uploading the same image inside one run costs nothing extra. Across runs it does, because each run has its own store.

## Persistence

There is none by design. Runs live on the container's ephemeral disk and a janitor deletes them past a retention window, along with any orphan directory left by a restart. Nothing is written to SQLite; the demo returns JSON only.
