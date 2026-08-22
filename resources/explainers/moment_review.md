# Moment Review

A set-level pass over the deterministic moment proposals. For each outing, the model reads the per-image
descriptions the visual LLM has already produced, as text, together with capture times, time gaps and metre
distances, and returns the moments it sees: which consecutive photos belong together, a short title and a
one-sentence reason for each, and any frame it reads as adding nothing. No image is sent, nothing is
re-evaluated, and the whole corpus costs cents.

## Why

Week 3 measured that the per-image model changed no moment boundaries: Shanghai pair F1 was 0.780 before and
after refinement because the model was never consulted on where one moment ends. It also found that exclusion
is a judgement about a set, not an image. The review is the pass that asks those questions of the set, using
the text output already on disk rather than paying to look at the photos again.

## What the model sees

One outing at a time, rendered as text:

```
Session of 13 photos in capture order, proposed as 6 moments. Times are UTC.
Day 2026-05-08
[0] 01:30 A traditional temple stands among modern high-rises | scene: landmark | keep: keep | quality: 0.82
[1] 05:40 (+4 h 10 min, 2.3 km) Golden statues line an ornate temple hall | ...
--- proposed boundary: 13 min gap, window 15 min ---
[2] 09:16 (+13 min, 180 m) Street signs for Zhongshan East 1st Road ... | landmark: The Bund, Shanghai | ...
```

Each frame line is the per-image description line (`ImageSignal.summary`, built by the reader for the record's
schema version), the UTC clock time, the gap since the previous frame and the distance moved when both photos
carry coordinates. The rule-based boundaries are marked with their reason. File names, coordinates, counts and
identities never appear: the per-image output already excludes the last two, and the renderer never writes the
first two.

## Sessions

Sessions are built from the baseline proposals, not from raw photos (`jo_pipeline/regroup.py`
`SessionBuilder`):

| Rule | Value |
| --- | --- |
| Proposals with no capture time | Never reviewed |
| Primaries only | Duplicate and burst frames stay attached to their canonical and are not listed |
| New session | A gap of more than 8 hours between consecutive proposals |
| Cap | 40 frames; an oversize session is cut at its largest gap between proposals, never inside one |
| Minimum | A session of one photo is not reviewed |

The session id is a hash of the frames' content hashes, capture times, gaps, distances, boundary markers and
description lines, plus the source record version and the review prompt and schema versions. Anything that
would change what the model sees changes the id; an unchanged outing is never reviewed twice.

## The verdict and how it is applied

The response is a strict JSON object (`llm_pipeline/schema.py` `MomentReview`): an ordered list of moments as
contiguous frame ranges that together cover every frame exactly once, each with a title of at most eight words
and a one-sentence reason, plus `leave_out` suggestions with a reason and confidence. A response that leaves a
gap, overlaps, starts late or stops short fails validation and is retried once with the error; a second
failure is stored as `invalid` and that session keeps its rule-based proposals.

`RegroupApplier` maps each moment back onto the baseline: its primaries are the frames in the range, attached
duplicates and bursts follow their canonical, and the proposal is assembled by the same `ProposalBuilder` the
grouper uses, so label, span, located members, distance and score components are computed the same way. The
evidence records what happened:

- `review.title`, `review.reason`, `review.change` (`unchanged`, `merged`, `split`, `resegmented`) and
  `review.baseline_labels`;
- `bridged_boundaries`, the rule-based boundaries the review joined across;
- `opened_by` / `closed_by` keep the rule-based boundary where it is unchanged and carry
  `{"kind": "review_split", "after": ..., "reason": ...}` where the review drew a new one.

Leave-out suggestions feed the existing refinement: the suggested frames' signal becomes `leave_out` with
`source: moment_review`, and `ProposalRefiner` drops them into `excluded_by_signal` exactly as it does for the
per-image verdict, reversible and visible. The review runs before refinement, so representative election and
drops happen on the final moments. The result is stored as `method_version` `regroup-1`, beside `group-1` and
`refine-1`, so every claim about it is a measured delta.

Sessions without a valid review pass their proposals through with `review.status: fallback`; the unanchored
proposal passes through as `skipped`.

## Reading the records that exist

`jo_pipeline` reads whichever evaluation run directory is named with `--llm-version` (default: the current
prompt and schema). The complete corpus is under `p1-llm-eval-1`, so that is what the benchmarks use.
`jo_pipeline/evaluation_readers.py` turns a v1 or v2 record into the same `ImageSignal`, including the summary
line; an unknown schema version is rejected rather than guessed.

## Measured on Shanghai, 2026-08-22

`review-moments` over the 61 photos: 8 sessions, 53 frames, 8 valid reviews on the first attempt, $0.053.

| Metric | Baseline | Refined | Regrouped |
| --- | --- | --- | --- |
| pair precision | 0.889 | 0.889 | 0.857 |
| pair recall | 0.696 | 0.696 | 0.783 |
| pair f1 | 0.780 | 0.780 | 0.818 |
| proposals | 32 | 32 | 29 |
| reference memories split | 5 | 5 | 3 |
| proposals merging memories | 1 | 1 | 2 |
| excluded photos grouped | 7 | 5 | 5 |

The two splits it healed are the ones the rules cut on the 150 m step: the Bund street pair 13 minutes apart
and the night skyline pair 13 minutes apart. The merge it added joins a street mural with a convenience store
12 minutes later that the traveler kept apart. It suggested no leave-outs, so the keep-signal recall and
false-positive rate are unchanged at 0.29 and 0.00. The garden-walk split (pavilion read as part of the temple
visit) and the university day split across 2h45 stayed as the rules drew them.

## Commands

```bash
uv run python -m jo_pipeline review-moments --dataset "Emiliano_s_Pictures_Shanghai 2026" --llm-version p1-llm-eval-1
uv run python -m jo_pipeline benchmark --dataset "Emiliano_s_Pictures_Shanghai 2026" --llm-version p1-llm-eval-1
```

`review-moments` is the only command in `jo_pipeline` that transmits anything; it prints the sessions and an
estimate first. `run`, `benchmark` and `persist` read cached review records under
`data/llm_runs/<dataset>/review-r<prompt>-<schema>/` and never call the API. The web demo runs the review as its
`reviewing` stage after refinement, shows each moment's title and reason, and marks moments the review merged
or split.
