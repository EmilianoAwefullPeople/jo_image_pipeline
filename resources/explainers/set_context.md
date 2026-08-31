# Set Context

**Status: designed, not implemented.** This describes intended behaviour so the design can be
reviewed before the code exists. Delete this line when it ships.

A quick survey pass over a sample of an uploaded set, turned into a short brief that is injected into
every per-image evaluation prompt as background.

## Why

`llm_pipeline` evaluates every photo in isolation: one API call per image carrying a 1024px
derivative and its EXIF capture time, nothing else (`llm_pipeline/runner.py:169`,
`llm_pipeline/prompts.py:17`). Calls run concurrently and independently, so no cross-image context
exists at prompt time by construction. The model never knows the photo belongs to a set.

That costs us on the fields the data-extraction categories call most valuable: landmark
identification (#3), specific environment and cultural style (#7), notable subjects (#4). A
sarcophagus photographed alone is "Artifact, carved stone sarcophagus". The same photo inside a set
that is visibly a trip to Egypt can be read against Cairo, Giza and Luxor, and produces a richer
value for postcards and recaps.

## The rule the whole design turns on

**Context is a prior, never evidence.**

The trust rules already in the system prompt — never invent, prefer null over lookalikes, confidence
must honestly reflect uncertainty — are why this subsystem's output is worth anything. Telling the
model "this is Egypt" is precisely the pressure that produces a confidently wrong landmark name on a
photo of an unremarkable stone wall.

So the per-image system prompt carries a **Set context rules** block alongside the trust rules:

- the brief is background inferred from a sample of the other photos in this set, sometimes helped by
  their recorded positions; it is not evidence about *this* photo;
- never name a landmark, place, subject or activity the context supplies but this image does not
  show;
- never raise `landmark.confidence_tier` because the context agrees;
- where the image contradicts the context, the image wins.

## How it works

One extra model call per set, before the per-image pass.

| | |
| --- | --- |
| Sample | 6 photos, evenly spaced across the path-sorted set |
| Derivative | 512px max edge, JPEG, EXIF stripped by the re-encode |
| Cost | ~$0.03 per set, flat, regardless of set size |
| Failure | Degrades to no context; the run continues exactly as today |
| Sets under 3 images | No request is sent |

Each sample is described to the model as `Photo 3 of 6, captured 2024:09:14 10:22:01, camera position
29.9773N 31.1325E.` Capture time and coordinates are each omitted independently when absent. GPS
presence is patchy across the sample archives — that is what the metadata reliability report
measures — so a set with no coordinates at all surveys on pixels alone.

The survey returns three fields under a strict schema:

| Field | Shape |
| --- | --- |
| `summary` | 20 to 40 words, what this set of photos appears to be |
| `places` | up to 5 candidate places, broad to specific, empty when nothing supports one |
| `themes` | 3 to 6 short recurring-subject or activity phrases |

The rendered brief carries place **names** and themes as plain text. It never carries coordinates.

Sampling walks the path-sorted list `discover_images` returns (`llm_pipeline/discovery.py:19`).
Path order is chronological for camera-named files, and reading capture time for the whole set would
mean opening every file — which the quick pass should not do.

A brief is written to `context.json` in the run directory and reused when one is already there, so a
resumed CLI run keeps the brief that produced its existing records instead of silently drifting.
Each image record carries the `set_context_id` that produced it.

## Privacy

The per-image calls are unchanged: a downscaled clean re-encode and the capture time, nothing else.

**What changes:** the one survey call per set also sends coordinates for up to 6 sampled photos,
rounded to 4 decimal places (~11 m) — enough to separate Giza from Saqqara, no more precise than the
job needs. `jo_pipeline` rounds to 7 for its own records; that is unchanged.

The claim in `llm_image_evaluation.md` that "capture time is the only metadata sent" becomes true of
per-image calls and false of the set as a whole, and has to be rewritten rather than left standing.

Coordinates never appear in a per-image prompt, and never in the generated brief. Blast radius is
6 coordinates per set, once.

## Dependencies

`llm_pipeline` stays independent of `jo_pipeline`. `DerivativeBuilder` already opens each file and
reads the EXIF IFD for `DateTimeOriginal` (`llm_pipeline/derivative.py:60`); it reads the GPS IFD
(`0x8825`) off the same open handle for no extra I/O.

The DMS-to-decimal arithmetic (three lines) is duplicated from `jo_pipeline/normalize.py:129-132`.
That is deliberate: the alternative is importing `jo_pipeline`, whose `_coordinate` is welded to
`RawExtraction`/`MetadataObservation` and only reachable through `PhotoExtractor`, which also runs
k-means, Laplacian blur and dHash on every sampled image. Three lines of arithmetic beats dragging
OpenCV into the survey path.

## Where it lands

| Area | Change |
| --- | --- |
| `llm_pipeline/derivative.py` | GPS read; `max_edge` constructor argument so the survey builds at 512px. `DERIVATIVE_VERSION` and the 1024px per-image path untouched |
| `llm_pipeline/schema.py` | `SetSurvey` model and `set_context_response_schema()`, reusing `StrictModel` and `_apply_strict_rules` |
| `llm_pipeline/context.py` | New. `SetContext`, `SetContextBuilder`, `resolve_set_context()` |
| `llm_pipeline/prompts.py` | `PROMPT_VERSION` 2 to 3; `{set_context}` placeholder; `PromptSet` gains the survey prompts and `build_context_messages()` |
| `llm_pipeline/client.py` | `complete()` takes `schema_name` and `schema` instead of hardcoding the image-evaluation one (`client.py:57`) |
| `llm_pipeline/store.py` | `read_context()`/`write_context()`; `records()` skips `context.json`; records carry `set_context_id` |
| `llm_pipeline/runner.py`, `cli.py` | Runner takes the context; CLI resolves it before the run and prices it in `estimate` |
| `jo_web/` | `surveying` stage, `set_context` on `RunState`, `llm.context` in the payload, read-only Set context panel |

Two sharp edges worth naming:

- `template_reject_reason` (`llm_pipeline/prompts.py:47`) rejects any template containing an unknown
  placeholder with a 400. It must gain `set_context` in lockstep with `build_messages`, or every
  custom prompt from the demo editor breaks.
- `jo_pipeline/cli.py:81 load_llm_records` hardcodes `PROMPT_VERSION`, so bumping to 3 means
  `jo-pipeline run` looks in `p3-llm-eval-2` and stops seeing p2 records. That is the established
  semantic of a version bump, not a regression. The 215 existing records under `p1-*`/`p2-*` are
  neither reused nor clobbered.

## Tests

New `tests/test_llm_context.py`, following the `httpx.MockTransport` pattern at
`tests/test_llm_evaluate.py:37` and the in-test image generation at `tests/test_web_service.py:69`:
sampling spread and cap; one image part per sampled photo and the strict survey schema pinned;
coordinates present and absent; `render()` never emitting coordinates; a set under the minimum
sending no request; an unparseable response yielding no context without raising.

Extended: `test_llm_derivative.py` (GPS read, southern and western hemisphere signs),
`test_llm_prompts.py` (placeholder rendering and the fallback string), `test_llm_store.py` (context
round-trip, `records()` ignoring it).

`tests/test_web_service.py` **needs updating** — `build_transport` returns an evaluation payload for
every request, and two tests assert exact request counts and payload ordering
(`test_web_service.py:101`, `:155`). The survey becomes request 0.

## Verifying it was worth doing

Capture a matched baseline **before** bumping `PROMPT_VERSION`.
`data/llm_runs/Dan Egypt 2024/p2-llm-eval-2/` currently holds only a couple of records, so run
`--limit 10` on the current code first.

```bash
uv run pytest
uv run python -m llm_pipeline estimate --dataset "Dan Egypt 2024"     # no network; checks the survey line
uv run python -m llm_pipeline run --dataset "Dan Egypt 2024" --limit 10
```

Then compare `landmark.name`, `landmark.confidence_tier` and `environment.specific_style` across
`p2-llm-eval-2` and `p3-llm-eval-2` for the same hashes.

Watch for the failure mode as hard as the win: **a landmark name that appears only in p3 with no
matching visible evidence is the context leaking in as fact**, and means the rules block is not
holding.

End to end in the demo: `uv run python -m jo_web`, upload the Egypt set, watch `surveying` land,
read the Set context panel, confirm the extracted fields shift on the landmark-bearing photos.
