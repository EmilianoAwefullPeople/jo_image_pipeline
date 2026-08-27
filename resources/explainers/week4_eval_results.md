# Week 4 Eval Results: Five Sets Against the Two-Variant References

Written 2026-08-27. The Week 4 direction's tasks 8 and 9 were blocked on Emiliano's five new sample sets and
their reference groupings; they arrived today as five photo folders under `resources/final_eval_sets/` and two
PDFs, a moment-based manual grouping and a content-based semantic grouping, each covering all five trips. This
document is the results report (task 8: cost, latency, accuracy, completeness) and the comparison report
(task 9: numbers plus a read). The grouping styles the same runs produced are shown side by side in
`resources/explainers/grouping_strategies.md`.

Until today every grouping number in this repo rested on Shanghai alone. Everything below is new evidence.

## The references as data

The benchmark reads references by matching reference images to dataset files; the existing reader understood
only `.docx`. The two PDFs render each reference photo as a cropped cell thumbnail with the reviewer's colour
scribble baked into the pixels, so `scripts/convert_pdf_references.py` parses the PDF layout (group headers and
image placements in reading order), decodes each embedded raster, and matches it to a dataset file by pixel
comparison that tolerates the scribbles: 32x32 downscales compared on the best 65% of pixels, over centre, edge
and full-frame crops, with a greedy assignment that keeps matches unique where the variant demands it. The
output is one JSON per trip per variant under `resources/final_eval_sets/references/`, which
`benchmark --reference` now reads directly.

Match quality: 340 of 340 placements resolved (149 manual, 191 semantic), one placement pinned by hand in
`match_overrides.json` after visual confirmation (a juice-box photo whose reference cell used a tight zoom
crop), and every match scoring above the visual-check line was verified by eye against the source file. Every
group's member count equals the count printed in its own header in the PDF.

One discrepancy to flag to JO: the PDFs' front summary table disagrees with their own detail sections. Acapulco
says 14 groups in the table but renders 19; Berlin says 14 and renders 24; Nazaré says 14 and renders 17;
Zurich says 14 groups and 25 grouped photos but renders 15 groups holding 26, the extra single-photo group
carrying what the table calls the one untagged photo. The detail sections are internally consistent, so they
are what the reference JSONs and every number below are built from.

## Task 8 - running the five sets through the polished system

Prompt v3 / `llm-eval-3`, `openai/gpt-5.6-sol` via OpenRouter, concurrency 4, then the moment review and all
five topic styles per set. Duplicate files are evaluated once, which is why Berlin's 39 files are 36 records.

| Set | Images evaluated | Valid | Invalid | Validation retries | Cost | Latency median | Latency p95 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| Acapulco | 29 | 29 | 0 | 4 | $0.272 | 14.9 s | 23.3 s |
| Berlin | 36 | 36 | 0 | 5 | $0.406 | 13.7 s | 30.5 s |
| Costa Rica | 35 | 35 | 0 | 2 | $0.408 | 14.3 s | 24.9 s |
| Nazaré | 19 | 19 | 0 | 1 | $0.234 | 15.1 s | 26.3 s |
| Zurich | 30 | 30 | 0 | 1 | $0.339 | 14.9 s | 26.8 s |

149 of 149 images returned a schema-valid evaluation; 13 needed the single validation retry the pipeline
allows, none needed more. Per-image evaluation for all five sets cost $1.66; the moment review plus all five
topic styles on all five sets added $0.73. The whole exercise, every number and every grouping in both
documents, cost $2.39 and about 12 minutes of wall clock per full set at concurrency 4.

Field population, counted over valid records (the completeness half of task 8):

| Field | Acapulco (29) | Berlin (36) | Costa Rica (35) | Nazaré (19) | Zurich (30) |
| --- | --- | --- | --- | --- | --- |
| 1 general_description | 100% | 100% | 100% | 100% | 100% |
| 2 scene_setting | 100% | 100% | 100% | 100% | 100% |
| 3 landmark | 0% | 3% | 3% | 32% | 17% |
| 4 notable_subjects | 100% | 100% | 100% | 100% | 100% |
| 5 focal_points | 100% | 100% | 100% | 100% | 100% |
| 6 activity | 83% | 69% | 54% | 53% | 87% |
| 7 environment | 100% | 100% | 100% | 100% | 100% |
| 8 composition | 100% | 100% | 100% | 100% | 100% |
| 9 weather | 100% | 100% | 100% | 100% | 100% |
| 10 keyword_tags | 100% | 100% | 100% | 100% | 100% |
| 11 photographic_style | 100% | 100% | 100% | 100% | 100% |
| extra why_tags | 86% | 92% | 97% | 100% | 93% |

Nine of the eleven fields populate at 100% across all 149 images. The two that do not are the two where null
and empty are defined as valid answers: `activity` is empty when no activity is depicted (a landscape, a plate
already eaten), and `landmark` is null unless something identifiable is visible. The landmark rates deserve the
task 7 conversation: Nazaré's 32% and Zurich's 17% are real identifications (Praça do Comércio, Miradouro do
Suberco, Lake Zürich, Fraumünster, ETH), Acapulco's 0% is honest for a resort trip with no named sights, but
Berlin at 3% got only the Berliner Dom, and photos of recognisable Berlin streetscapes stayed null. The prompt's
trust rules trade recall for never inventing a name; these five sets now measure that trade.

## Task 9 - moments against the manual reference

`benchmark` compares baseline (`group-1`), refined (`refine-1`) and regrouped (`regroup-1`, the moment review)
against the manual reference. Pair precision counts proposed same-group pairs the traveler also grouped; recall
counts the traveler's pairs the pipeline reproduced.

| Set | Baseline P / R / F1 | Regrouped P / R / F1 | Proposals | Reference groups split | Proposals merging groups | Excluded photos still grouped |
| --- | --- | --- | --- | --- | --- | --- |
| Acapulco | 1.000 / 0.500 / 0.667 | 1.000 / 0.667 / **0.800** | 24 → 22 | 5 → 3 | 0 → 0 | none excluded in reference |
| Berlin | 0.706 / 0.706 / 0.706 | 0.778 / 0.467 / **0.583** | 25 → 27 | 2 → 4 | 2 → 2 | 1 → 0 |
| Costa Rica | 1.000 / 0.500 / 0.667 | 1.000 / 0.750 / **0.857** | 30 → 28 | 2 → 1 | 0 → 0 | 4 → 3 |
| Nazaré | 0.250 / 0.500 / 0.333 | 0.250 / 0.500 / **0.333** | 16 → 16 | 1 → 1 | 1 → 1 | none excluded in reference |
| Zurich | 0.900 / 0.643 / 0.750 | 0.929 / 0.929 / **0.929** | 18 → 15 | 3 → 1 | 1 → 1 | 4 → 3 |

Refinement moved no boundary on any set, exactly as measured on Shanghai in weeks 3 and 4; the per-image pass
re-elects representatives and drops leave-outs but was never asked where a moment ends. The set-level review is
where boundaries move, and on these five sets it moved them in both directions:

- **Three sets clearly improved.** Zurich is the strongest grouping result the pipeline has produced anywhere:
  0.929 precision and recall, three rule-based splits healed down to one, with the review joining the airport
  arrival to the lakefront and the snowy woodland walk to the mountain overlook picnic. Acapulco and Costa Rica
  both went from F1 0.667 to 0.800 and 0.857 while keeping precision at 1.000: every pair the review proposed,
  the traveler had grouped.
- **Berlin got worse, and the reason is the finding.** The review split four moments the traveler kept whole:
  a walk through leafy streets from the café dessert that ended it, and the bouldering session from the
  post-climb beer. This traveler's manual groups bundle an activity with its follow-on reward; the review's
  question, "what happened, in order", cuts at the scene change. The Memories style run on the same photos
  reproduced the traveler's own framing, titling one group "Bouldering and a Post-Climb Beer" and another
  "A Leafy Walk with Café Treats". Which reading is right is not a model quality question, it is per-traveler
  taste, and it is exactly what the style drop-down exposes.
- **Nazaré's numbers carry almost no signal.** The traveler grouped 19 photos into 17 groups, leaving two
  2-photo pairs; the entire pair metric rests on four pairs. The review left the rules' boundaries alone. The
  set is much more informative about styles (it is the only set with strong landmark identification) than
  about moment boundaries.

The keep signal against the travelers' own exclusions, where the reference has any:

- **Berlin** (1 excluded): the per-image signal caught it, a utility screenshot, with one false positive (a
  practical gear shot the traveler kept). Recall 1.00, false-positive rate 0.029.
- **Costa Rica** (4 excluded): the per-image signal caught the internet meme and missed three moody
  young-palms beach shots the traveler crossed out; the model rates them "distinctive sense of place" with
  confidence around 0.9. Recall 0.25, false-positive rate 0.00. The review suggested nothing further.
- **Zurich** (4 excluded, all near-identical overlook shots): the per-image signal caught none, because each
  photo alone is a keeper; the moment review, seeing the outing as a set, caught one as "a near repeat of
  frame 7, taken five seconds later". Recall 0.25 with the review, false-positive rate 0.00.

The pattern from Shanghai holds: the signal reliably catches non-photos (screenshots, memes), and exclusion
that expresses taste between similar keepers is a set-level judgement the per-image pass cannot make and the
review only partially recovers.

## The topic styles against the semantic reference

The semantic reference groups by content, across time, with photos repeating between groups, and Emiliano's
note frames it as a sanity check on what the extraction fields should surface. The pair comparison treats two
photos as together when they share at least one reference group. Regrouped pair scores per style:

| Set | Memories | Landmark tour | Foodie tour | Location highlights | Enjoyable moments |
| --- | --- | --- | --- | --- | --- |
| Acapulco | 0.349 | no groups produced | 0.136 | 0.345 | 0.315 |
| Berlin | 0.099 | 0.000 (1 single-photo group) | 0.057 | 0.000 | 0.341 |
| Costa Rica | 0.320 | 0.000 (1 single-photo group) | 0.000 (7 single-photo groups) | 0.255 | 0.398 |
| Nazaré | 0.324 | 0.000 (5 groups, 6 photos) | 0.000 (4 single-photo groups) | **0.609** | 0.393 |
| Zurich | 0.240 | 0.000 (4 single-photo groups) | 0.182 | 0.345 | 0.340 |

Read these with the metric's shape in mind: pair scores ignore single-photo groups entirely, so a style that
correctly produces many one-photo groups (a foodie tour where every meal was photographed once, a landmark tour
with one photo per sight) scores zero by construction, not by being wrong. Nazaré's landmark tour named five
real places correctly and still scores 0.000. The two styles whose groups are naturally multi-photo track the
semantic reference best: Enjoyable moments lands 0.32-0.40 on every set because the reference's own categories
(beach scenes, sunsets, nightlife) are close to moods, and Location highlights peaks at 0.609 on Nazaré where
the reference's split is literally Lisbon versus Nazaré. The per-group membership read, and what each style
actually produced, is in `grouping_strategies.md`; the fair conclusion here is that the semantic reference
validates the extraction fields' content coverage, and judging the styles as products needs the eye, not this
pair metric.

## What it means for JO

- **The set-level review is the grouping lever, and it is now measured on six travelers.** It improved three of
  five new sets, one dramatically (Zurich 0.929), left a tiny set alone, and hurt one (Berlin) by cutting
  activity-plus-reward arcs this traveler kept whole. Moments boundaries are a taste dial, not a solved
  constant, and the style drop-down is the mechanism for letting the traveler pick the reading.
- **Reliability is a landmark problem, not a schema problem.** 100% schema-valid responses across 149 images,
  nine fields at 100% population, and the only weak field is landmark identification under
  never-invent rules. Task 7 work should aim there, and the set-context design
  (`resources/explainers/set_context.md`) is the designed-but-unbuilt candidate.
- **Cost and latency are a rounding error at this scale.** About 1.1 cents per photo for the full extraction,
  half a cent per photo for every grouping style at once, 14 seconds median per photo, fully cached and
  resumable. A 35-photo trip lands in 2-3 minutes.
- **The reference PDFs' summary table is wrong for four of five trips** (group counts, and Zurich's photo
  count); the detail sections are self-consistent and were used. Worth a line to Emiliano before anyone quotes
  the table.

## Reproducing

```bash
export JO_DATASET_ROOT="$(pwd)/resources/final_eval_sets"
uv run python scripts/convert_pdf_references.py
uv run python -m llm_pipeline run --dataset "Zurich"
uv run python -m jo_pipeline review-moments --dataset "Zurich" --style moments
uv run python -m jo_pipeline benchmark --dataset "Zurich" --reference "resources/final_eval_sets/references/Zurich_moments.json"
uv run python -m jo_pipeline benchmark --dataset "Zurich" --style memories --reference "resources/final_eval_sets/references/Zurich_semantic.json"
uv run python scripts/eval_set_report.py --dataset "Zurich"
```

`benchmark` and the report never call the API. Every artifact behind the tables above is committed under
`resources/final_eval_output/`: per-set benchmark JSONs (moments, and one per topic style, carrying every
proposal with its evidence and review titles) and the per-set results JSONs with cost, latency and field
population.
