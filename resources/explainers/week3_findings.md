# Week 3 Findings: Visual LLM Evaluation

Started 2026-08-17. This document is being written as Week 3 progresses; sections marked unmeasured will be
filled in as runs complete. Week 2 established what the local pipeline can extract without any external call.
Week 3 measures what a visual LLM adds, at what cost, and under what controls.

## Scope and method

Stages 6 and 7 of the pipeline hypothesis (Infer and Validate) are implemented as a standalone subsystem
(`llm_pipeline/`) that still makes every external call and owns every paid artifact: it shares the `.env`
configuration and the dataset folders with the local pipeline and imports nothing from it, and it writes only
its own immutable JSON records.

Consuming those records is now `jo_pipeline`'s job rather than nobody's. `jo_pipeline/refine.py` reads the
plain evaluation dictionaries and feeds grouping, and `jo_pipeline/persist.py` writes them to SQLite. The
boundary is one-directional and deliberate: `refine.py` imports nothing from `llm_pipeline`, so the deterministic
pipeline never depends on the paid subsystem being present, and a dataset with no evaluations still produces a
complete result.

Each image gets one structured call to the baseline model through OpenRouter. The request pins the model route
(`allow_fallbacks: false`) and denies provider data collection, demands strict JSON-schema output, and asks for
usage accounting so the provider's own cost figure is recorded per call. The response is re-validated locally
with pydantic; a validation failure is sent back once with the error attached, and a second failure is stored
for review rather than retried further. Transport-level failures (429 and 5xx) are retried up to three times
with exponential backoff, which is a separate concern from validation and does not consume the single
validation retry. Requests run four at a time; byte-identical files are evaluated once per content hash.

| Component | Version |
|-----------|---------|
| Model | openai/gpt-5.6-sol via OpenRouter |
| Prompt | v1 |
| Response schema | llm-eval-1 |
| Derivative method | derivative-1 |
| Python | 3.14.5 |
| httpx | 0.28.1 |
| pydantic | 2.13.4 |
| Pillow / pillow-heif | 12.3.0 / 1.5.0 |

A fourth dataset was added this week: Dan Egypt 2024, 27 JPEG images, not covered by any Week 2 manifest or
measurement and carrying no reference grouping.

## What the model is asked to judge

The schema covers the fields Week 1 assigned to the visual model plus the two gaps Week 2 measured. All of
them are judgments a deterministic CV pipeline cannot make:

- caption, scene classification and activity interpretation (constrained vocabularies, evidence required)
- landmark candidate: a hypothesis with evidence, never a fact; null preferred over lookalikes
- meaningful visible text: signs, menus, tickets, in the original language
- screenshot/document judgment with travel relevance, because a boarding pass is relevant media and a stray
  screenshot is not; flagged for review, never deletion
- emotional salience using the traveler's own vocabulary from the Shanghai reference document: awe,
  excitement, meaningful, calm, fun, connection
- a keep / leave_out / unsure memory signal, aimed directly at the Week 2 finding that all 7 matched photos
  the traveler intentionally excluded were grouped anyway because exclusion was not modelled
- representative quality scored on composition, story and expressiveness, explicitly not sharpness, targeting
  the file-size and Laplacian-variance proxies the current scoring admits are weak

People counting and identity are excluded by construction: the schema has no fields for them and the prompt
forbids them in free text, per the privacy gating in the Week 1 reliability matrix. Null is a valid outcome
for every inferred field, and every non-null inference must carry confidence and evidence.

## What leaves the machine

Originals never leave the workstation. Each image is re-encoded to a JPEG capped at 1024 pixels on the long
edge at quality 85, which strips all EXIF including GPS as a property of the re-encode rather than of tag
deletion. Measured on IMG_0591: a 2,350,403-byte original became a 768x1024 derivative of 130,797 bytes. The
only metadata transmitted is the EXIF capture time, sent as one line of text context. The API key is read from
`.env` server-side; nothing else identifies the traveler.

## First live measurements

First external call of the engagement was made on 2026-08-17 against 2 images from Dan Egypt 2024.

| Image | Tokens in | Tokens out | Latency | Cost | Validation |
|-------|-----------|------------|---------|------|------------|
| IMG_0591.jpeg | 2,373 | 457 | 9.4 s | $0.0285 | valid, first attempt |
| IMG_0599.jpeg | 2,373 | 721 | 12.6 s | $0.0283 | valid, first attempt |

Both responses parsed and validated on the first attempt, so the strict-schema path has yet to need its retry.
Input token counts were identical because both derivatives tile to the same size; roughly 1,550 of the 2,373
input tokens are the image and the remaining ~800 are the system prompt and the JSON schema that ride along on
every request.

Output quality on the first sample behaved the way the prompt demands rather than the way vision models
default to behaving:

- The nighttime bridge portrait was classified people_moment with no count or identity anywhere in the output,
  including free-text fields.
- The landmark field returned null at 0.18 confidence with the evidence "no uniquely identifiable landmark can
  be confirmed", rather than guessing a plausible name.
- Emotion labels stayed inside the constrained vocabulary (connection, meaningful, fun) with per-label
  confidence.
- The representative score of 0.9 was justified by pose, sense of place and composition, with no reference to
  sharpness or resolution.
- The journaling prompt ("What made this nighttime stop by the water memorable for you?") is usable as-is for
  the app's unused ai_prompts field.

Two images prove the mechanism, not the accuracy. The full-corpus run below supplies the accuracy.

## Full corpus run

Run on 2026-08-17 across all four datasets, four requests in flight, resuming from the two records above.

| Dataset | Evaluated | Valid | Invalid | Request failures | Cost |
|---------|-----------|-------|---------|------------------|------|
| Emiliano_s_Pictures_Shanghai 2026 | 61 | 61 | 0 | 0 | $1.4788 |
| Glenn_London | 99 | 99 | 0 | 12 | $2.6522 |
| Dan Egypt 2024 | 27 | 27 | 0 | 0 | $0.6931 |
| Glenn_Pictures_Cruise_2024 | 20 | 20 | 0 | 0 | $0.4856 |
| **Total** | **207** | **207** | **0** | **12** | **$5.3097** |

**Every one of 207 responses validated on the first attempt.** The single validation retry has still never
fired in a live run across 207 calls, which says the strict JSON-schema route plus the local pydantic
re-validation agree in practice rather than merely in principle.

The 12 failures are all one cause: **HTTP 402, the OpenRouter account exhausted its credit** partway through
the last dataset. `IMG_4518` through `IMG_4537` of Glenn_London are unevaluated for that reason and no other.
402 is deliberately absent from the retry list, so the runner gave up immediately on each rather than spending
three attempts discovering the same thing; the failures are recorded and the run continued to a clean summary.
Topping up and re-running Glenn_London will evaluate exactly those 12, because records are content-hash keyed
and the other 99 are skipped without spending.

Cruise is the clearest demonstration of the duplicate handling: **21 images carry evaluations from 20 model
calls**, because the `IMG_5348.HEIC` / `IMG_5348(1).HEIC` exact-hash pair Week 2 identified was evaluated once
and the single record fanned out to both paths.

## Cost

Measured cost across 207 images is **$0.02565 per image**, half the pre-run estimate of $0.053, because actual
output ran 457 to 721 tokens against the 1,500 assumed. The output side still dominates: at $5 per million
input and $30 per million output tokens, the structured response costs more than the image that prompted it.

| Measured at $0.02565/image | Cost |
|----------------------------|------|
| Corpus evaluated so far (207) | $5.31 |
| Glenn_London remainder (12) | ~$0.31 |
| A 500-photo trip | ~$12.83 |
| A 500-photo trip at 768px derivatives | ~$9 to $10 (untested) |

One accounting finding: the provider-reported cost diverges from list-price token arithmetic in both
directions on the same run ($0.0285 reported against $0.0256 computed on one image, $0.0283 against $0.0335
on the other). The recorded figure is always the provider's own `usage.cost`, and recomputing from token
counts should not be treated as a check against it.

## Records and reproducibility

Every evaluation is an immutable JSON record keyed by the image's SHA-256 under
`data/llm_runs/<dataset>/p<prompt_version>-<schema_version>/`, carrying the model, prompt, schema and
derivative versions, token counts, provider cost, latency, attempt count and either the parsed evaluation or
the full failure detail. Re-running a dataset skips anything already recorded, so an interrupted run resumes
without re-spending, and bumping the prompt or schema version writes into a fresh directory instead of
overwriting the old results. Each invocation also writes a timestamped run summary with totals.

## Wired into grouping, and what it actually changed

Until now the evaluation subsystem ran beside the pipeline and nothing consumed it. It is now wired in through
`jo_pipeline/refine.py`, which takes the deterministic proposals plus the per-image evaluations and applies
three changes: it drops members the model marks `leave_out`, re-elects each representative on the model's
`representative_quality` instead of Laplacian variance, and replaces the mean-sharpness term in the group score
with mean model quality. Screenshots judged not travel relevant are flagged in evidence and never dropped.

`MomentGrouper` is untouched, so the deterministic baseline still exists and every claim below is a measured
baseline-against-refined delta rather than an assertion. `jo_pipeline benchmark` produces it with no API calls.

### Grouping accuracy: the model changed nothing

Scored against the Shanghai traveler's own grouping, the only dataset carrying a reference document:

| Metric | Baseline | Refined | Delta |
|--------|----------|---------|-------|
| pair precision | 0.889 | 0.889 | +0.000 |
| pair recall | 0.696 | 0.696 | +0.000 |
| pair f1 | 0.780 | 0.780 | +0.000 |
| proposals | 32 | 32 | 0 |
| reference memories split | 5 | 5 | 0 |
| proposals merging memories | 1 | 1 | 0 |
| intentionally excluded photos still grouped | 7 | 5 | **-2** |

**The visual model does not improve moment boundaries.** That is the honest headline. Pair metrics are driven
by which photos land together, and the model is not consulted on that; it only edits membership at the margin
and reorders representatives. Week 2's diagnosis stands unchanged — recall is the weak side, splits outnumber
merges five to one, and the lever is the 45-minute time window, not AI vision.

### The keep signal is precise but far too insensitive

This is the metric built specifically to close the Week 2 finding that all 7 matched photos the traveler
deliberately left out were grouped anyway.

| Measure | Result |
|---------|--------|
| Excluded photos evaluated | 7 of 7 |
| Caught as `leave_out` | 2 |
| **Recall** | **0.29** |
| False positives on 44 kept photos | 0 |
| **False positive rate** | **0.00** |

It never wrongly discards a photo the traveler wanted, and it catches under a third of the ones they cut. The
five misses were all marked `keep` with high confidence (0.86 to 0.97), so this is not hesitancy at the margin —
the model is confidently wrong about them.

Corpus-wide the bias is starker: **`leave_out` fired on 2 of 207 images**, and zero times across Dan Egypt,
Cruise and London. The signal as prompted at v1 is close to a constant `keep`.

Two structural reasons, both visible in the data rather than inferred:

- **None of the 7 excluded photos is a duplicate or burst frame.** The deterministic dHash index classified
  every one of them as a distinct visual asset, so this is not redundancy the model failed to spot.
- **Exclusion is a judgement about a set, not about an image.** Two of the five misses sit in the same
  proposal, and two more in another. The model sees one photo at a time with no knowledge of the other frames
  or of the trip narrative, so it cannot know a photo is the weaker of two similar shots or that it does not
  belong to any memory worth keeping. A per-image call is the wrong shape for this question.

The implication for Week 4: closing this gap is a prompt-and-architecture change, not a tuning knob. Either the
model is shown the moment as a set and asked which frames belong in it, or exclusion stays a traveler action
that the system records and learns from. On current evidence the second is the safer product answer, and the
0.00 false-positive rate means the signal is still usable as a *suggestion* even at 0.29 recall.

### Representative selection is where the model earns its place

The model disagrees with sharpness often, and where there is ground truth it is right.

| Dataset | Moments | Representative re-elected |
|---------|---------|---------------------------|
| Shanghai | 32 | 9 |
| Glenn_London | 39 | 17 |
| Dan Egypt 2024 | 16 | 3 |
| Cruise | 14 | 1 |
| **Total** | **101** | **30 (30%)** |

The one case with an objective answer is decisive: in the baseline, one Shanghai moment elected as its
representative a photo the traveler had explicitly listed as intentionally left out — the sharpest frame in
that moment was one they did not want. **After refinement, zero moments have an excluded photo as their
representative.** One data point, but it is the only data point available and it points the same way as the
Week 2 note that sharpness is a weak proxy for a photo worth showing.

### Persistence: the schema now holds the model output

`model_calls` and `review_decisions` had existed unwritten since Week 2. Evaluations now persist with **no DDL
change**, using the `fetched` category the `metadata_observations` CHECK constraint already reserved for
externally obtained metadata. Verified end to end on Shanghai:

| Table | Rows |
|-------|------|
| media_assets | 62 |
| model_calls | 61 |
| group_proposals | 64 (32 `group-1` + 32 `refine-1`) |
| group_members | 120 |
| metadata_observations | 1,647 (671 photo, 610 fetched, 244 detected, 122 computed) |

Baseline and refined proposals are stored as separate records distinguished by `method_version`, which is the
provenance model the schema header describes. `processing_runs.model_id` is populated for the first time. The
two version namespaces are kept apart deliberately: the run row carries the local pipeline's schema and prompt
versions, while each `model_calls` row carries the LLM's own (`1` and `llm-eval-1`).

Three schema gaps found by writing to it, none blocking:

- `model_calls` has no column for `generation_id` or `derivative_version`, both of which the JSON records
  carry. Adding them is a Week 4 migration, not a redesign.
- `request_bytes` has no source, because the derivative's byte size is computed in `derivative.py` but not
  carried onto the record. Written as null.
- `validation_status = 'error'` is never produced. Transport failures write no record at all, so the 12 credit
  failures above are invisible to the database — the `'error'` slot is exactly where they belong.

## Open items

- **12 of 220 images unevaluated**, blocked on OpenRouter credit (HTTP 402). Top up and re-run Glenn_London;
  it will evaluate exactly those 12 for ~$0.31 and skip the other 99.
- **Comparison against the app's current grouping output is not done.** It is a Week 3 and a Week 4 item and it
  needs Journey Onward's own grouping output for at least one of these datasets, which we do not have. This is
  the one Week 3 direction that cannot be closed from this side and should be raised with Emiliano.
- **Only Shanghai has a reference grouping**, so every accuracy number above rests on one dataset of 61 images
  and 30 reference memories. A reference grouping for London or Cruise would roughly triple the evidence base
  and is cheap for the team to produce.
- Landmark and scene accuracy are still unmeasured. 207 evaluations now exist to check against the cruise
  itinerary (Greece, Italy, Croatia) and London; this is reading, not computation.
- gpt-5.6-sol remains the quality reference, not the assumed production model. With 207 accepted outputs
  recorded, benchmarking a cheaper model against them is now possible and is the obvious cost lever after the
  derivative size.
- The 1024-pixel derivative is still the first guess. A 768-pixel comparison would cut image tokens roughly 40
  percent; at $0.0257 per image that is the difference between ~$12.83 and ~$9.50 for a 500-photo trip.
- The validation-retry path has never fired in 207 live calls. The transport-retry path fired only on 402,
  which it correctly refuses to retry. Neither is exercised by real load yet.
- Representative selection has exactly one ground-truth data point. A short traveler review of the 30 re-elected
  representatives against the baseline choice would settle whether the 30 percent disagreement is an improvement
  or noise, and it is the cheapest high-value measurement left.
