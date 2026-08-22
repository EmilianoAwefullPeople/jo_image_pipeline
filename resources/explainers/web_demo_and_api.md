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

## Build the set, then press Process

Uploading and processing are separate steps. A drop on the page opens a run (`POST /api/runs`) and sends each file to it one request at a time; the run stays in `created` while the visitor adds more, and the page shows everything accepted so far as a grid of thumbnails with the file name and size under each. Nothing is analysed until the Process button is pressed, which attaches any edited prompt and calls `POST /api/runs/{id}/start`. From that point the run is closed: further uploads and prompt edits answer 409, and the next drop on the page opens a fresh run.

The grid needs server-side thumbnails, because most customer media is HEIC and browsers will not render it. Each accepted upload is therefore hashed as it streams to disk and thumbnailed straight away under `thumbnails/<sha256[:16]>.jpg` — the same key the manifest will assign it later — and the upload response carries that key. The `thumbnailing` stage then only fills gaps, which in practice means files that reached `input/` without going through the upload route.

## Stages

`created` → `queued` → `inventorying` → `extracting` → `thumbnailing` → `grouping` → `evaluating` → `refining` → `complete`, or `failed` with a `failure_detail`. The client polls `GET /api/runs/{id}` for the current stage and results.

The run payload carries both `groups` and `baseline_groups`. `baseline_groups` is what capture time, GPS and OpenCV alone produced; `groups` is that result after the visual model re-elected representatives on composition rather than sharpness, dropped anything it read as not worth keeping, and flagged screenshots without removing them. Showing both is the point — it is the only way a viewer can see what the model contributed rather than taking it on trust. When no API key is configured the two are identical and the demo still works, returning metadata and grouping only.

Photos the model drops are listed in the proposal's `evidence.excluded_by_signal` with the model's reason and confidence, and rendered under their moment rather than hidden, so a viewer can disagree with the call.

Every proposal also carries the reason it exists, and the page prints it under the moment's heading. The grouper records the facts rather than a sentence: `closest_call` is the gap that came nearest to breaking the moment and the adaptive window in force at that point, `opened_by` and `closed_by` are the boundaries on either side (`time_gap` with the gap and window, or `place_change` with the distance and the threshold), and `place_threshold_metres` travels with the proposal so the page never hard-codes it. The boundary that closes one moment is the same object that opens the next, so the two agree by construction. Members attached as a duplicate or burst frame carry the photo they were attached to, the hash distance and the seconds between them.

Reporting the *closest* gap rather than the widest is deliberate: the window narrows with shooting cadence, so two equal gaps are not equally close to a boundary, and the one that nearly split the moment is the one worth showing.

Thumbnails are pre-built rather than transcoded per request. A full HEIC decode peaks near 250 MB — a gallery of them loading in parallel would exhaust the container. Serving pre-built JPEGs keyed by content hash also means the thumbnail route never builds a path from client input.

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

## Editing the prompt from the page

The demo page carries a prompt editor so a prompt change can be tried against real media without a redeploy. It is a testing feature and behaves like one:

- `GET /api/prompt` serves the system prompt and user template that ship with the build, along with the `{capture_local_time}` placeholder name and the character cap.
- `PUT /api/runs/{id}/prompt` attaches an edited pair to one run, before it starts. After `start` the route answers 409 — swapping the prompt mid-run would make the stored records lie about what was sent.
- The page only sends the prompt when the text differs from the served default, so an untouched editor leaves the run on the shipped prompt.

An edited prompt is validated before it is accepted: both parts must be non-empty and within the cap, and the user template must be formattable, since a stray brace would otherwise fail once per image at request time. `{capture_local_time}` is optional — dropping it simply means the model is not told the capture time.

A run carrying an edited prompt records `prompt_version` as `custom` rather than the shipped version, in its per-image records, its run summary, and the directory those records are written to. Nothing about the edit is persisted beyond the run: the next run starts from the shipped prompt again, and the files under `llm_pipeline/prompts/` are never written to. Changing the prompt changes what the model returns, so the moments a run proposes are only comparable with another run on the same prompt version.

## Duplicates

Byte-identical files are kept on disk and both reach grouping, where `DuplicateIndex` uses the shared hash as a first-class signal and collapses the later copy. Only the **evaluation** list is deduplicated by hash, so an identical pair is described once and paid for once, and the single record is shared by every path with that hash.

## Cost and exposure

The deployed demo has no authentication and evaluation is always on, so anyone with the URL can spend OpenRouter credit. The caps above are the only brake. Cost, token counts and per-image failures are reported in every run payload so spend is visible rather than inferred.

Records are keyed by content hash, so re-uploading the same image inside one run costs nothing extra. Across runs it does, because each run has its own store.

## Persistence

There is none by design. Runs live on the container's ephemeral disk and a janitor deletes them past a retention window, along with any orphan directory left by a restart. Nothing is written to SQLite; the demo returns JSON only.
