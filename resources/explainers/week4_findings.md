# Week 4 Findings: LLM-Informed Grouping, and What a Memory Is

Started 2026-08-22. Week 3 closed with the honest headline that the visual model did not change a single moment
boundary: per image, it was never asked where one moment ends. This document covers the Week 4 direction's
task 6, folding the LLM output into grouping as a layer on top of time and location, the measured result, and
the finding that came out of reading the traveler's own memory document against what the system now produces.

## What was built

The deterministic grouper (`group-1`) and the per-image refinement (`refine-1`) are untouched. A third,
separately versioned pass (`regroup-1`) asks the model about the set rather than the photo:

- **The input is text, not images.** For each outing, the per-image descriptions the visual model already
  produced are rendered in capture order with the UTC clock time, the gap since the previous photo, the metres
  moved when both photos carry a coordinate, and the rule-based boundary marked where the grouper drew one.
  No image is re-sent, no coordinate or file name appears, and nothing is re-evaluated. The complete corpus on
  disk is the v1 output, and the pipeline now reads whichever evaluation run is named with `--llm-version`, so
  the 219 existing records were used as they are.
- **An outing is a session.** Proposals within eight hours of each other form one session, capped at 40
  photos and cut only between proposals. Duplicate and burst frames stay attached to their canonical. A
  session of one photo is not reviewed.
- **The verdict is a strict partition.** The model returns contiguous frame ranges covering every frame
  exactly once, each with a title of at most eight words and a one-sentence reason, plus optional leave-out
  suggestions with a reason and confidence. A response that leaves a gap or overlaps fails validation, is
  retried once with the error, and on a second failure the session keeps its rule-based proposals.
- **The verdict is applied by the grouper's own code.** Each reviewed moment is assembled by the same
  `ProposalBuilder` the grouper uses, so span, located members, distance and score components are computed
  the same way; the evidence records the title, the reason, which rule-based moments it was merged from or
  split out of, and the boundaries it bridged. Leave-out suggestions go through the existing refinement with
  `source: moment_review`, reversible and visible like the per-image verdict. The review runs before
  refinement so representatives are elected on the final moments.
- **Every session is cached** under a hash of exactly what the model saw, so an unchanged outing is never
  paid for twice, and `review-moments` is the only command in `jo_pipeline` that transmits anything.

Full detail is in `resources/explainers/moment_review.md`.

## Measured result

Shanghai, the only dataset with a reference grouping: 8 sessions, 53 photos reviewed, 8 valid reviews on the
first attempt, $0.053.

| Metric | Baseline | Refined | Regrouped |
| --- | --- | --- | --- |
| pair precision | 0.889 | 0.889 | 0.857 |
| pair recall | 0.696 | 0.696 | **0.783** |
| pair f1 | 0.780 | 0.780 | **0.818** |
| proposals | 32 | 32 | 29 |
| reference memories split | 5 | 5 | **3** |
| proposals merging memories | 1 | 1 | 2 |
| intentionally excluded photos still grouped | 7 | 5 | 5 |

This is the first measured movement in moment boundaries from the model. The two splits it healed are the two
the 150 m place rule caused: a pair of street photos on the Bund 13 minutes apart, and a pair of night skyline
photos 13 minutes apart, each read by the model as one continuing walk. The merge it added joins a street
mural with a convenience store photographed 12 minutes later, which the traveler kept apart; that is the
precision cost. It suggested no leave-outs on Shanghai, so the keep-signal figures are unchanged at recall
0.29 and false-positive rate 0.00.

The two remaining splits it left alone are instructive and are the subject of the next section: a garden walk
whose last photo, a pavilion, was read as the start of the temple visit that followed it (the same reading the
rules made), and a university day whose afternoon still-life sits 2 h 45 min after the lecture photos.

Without a reference the other sets can only be read, not scored:

| Dataset | Sessions | Rule-based moments | Regrouped | Changes | Left out by review | Cost |
| --- | --- | --- | --- | --- | --- | --- |
| Glenn_London | 6 | 39 | 35 | 6 merged, 10 split, 3 re-cut, 15 unchanged | 4 | $0.076 |
| Glenn_Pictures_Cruise_2024 | 5 | 14 | 14 | 1 merged, 2 split, 9 unchanged | 0 | $0.022 |

London is where the set-level view shows most: the 18-photo moment that Week 2 flagged as spanning 729 m
without any single step breaking the place rule is now several moments ("Winchester Cathedral exterior and
interior", "Historic Kingsgate gateway", "Wenzel's bakery stop"), and all four leave-outs are near-repeat
frames one to nine seconds after their neighbour that the hash index had not caught. The Cruise titles are
sound and drawn from the frame text ("Arch of Hadrian sightseeing", "Selfie in Piazza San Marco").

Corpus cost for the whole pass was $0.15. Re-running it is free until the prompt, the per-image descriptions
or the rule-based boundaries change.

## Moments are not memories

The review improves the boundaries, but reading its output next to the traveler's document shows that it is
still answering a narrower question than the product asks. The Shanghai document does not only group photos;
its "Memories unpacked" section explains three of them with a fixed framework, **scene, why it mattered, and
what was happening beyond the images**, with the why drawn from a closed vocabulary: Awe, Excitement,
Meaningful, Calm, Fun, Connection.

Set against the system's output:

- The photos the review titled "Lanterns along a wooded trail" and, separately, "Wooded Buddhist temple
  complex" are one memory to the traveler: a hike to a temple by an unofficial route, nervous on the way up,
  relieved on arrival, remembered as an adventure. The per-image text even carried the clue, a fence with a
  no-climbing notice, but the question put to the model was "same place?" and the answer was no.
- A walk from a market across the city to the financial district, with music and time alone, ending in the
  skyline at sunset, is one memory. A place-based grouper splits a walk across places by construction.
- The photos the review titled "Coffee stop at Pudong Airport" are, to the traveler, coffee with a friend they
  became close to on the trip. The place is the label; the memory is the relationship.

In numbers: of the 27 reference memories the pipeline can match, 15 are a single photo, 5 cross a rule-based
boundary, and one (a convenience store one night and the skyline the next) crosses a day. The why and the
"beyond the image" are largely invisible in the pixels, which is why the traveler supplies them, but the
*shape* of a memory, which photos belong to it and roughly what it is about, is often discoverable from the
set, and that is exactly the question the set-level review is positioned to ask.

Three things in the current pass hold it to "I was in this place":

1. The prompt defines a moment as "a meal, a walk through one place, a visit to one site, or a view from one
   spot".
2. A moment is a contiguous range of frames inside an outing of at most eight hours, so nothing that spans
   places or days can be expressed.
3. The output is a place-style title and a reason; there is no why, and no notion of a photo belonging to no
   memory at all.

The Week 4 direction already names both sides of this. Task 6, the time-and-location layer, is what was built
and measured above. Task 11, the topic-based grouping path, is the memory question, and the reference sets
Emiliano is preparing come in two variants per sample set, moment-based and non-time/location-based, which is
the right evidence to measure both against.

## What was done about it

Two things, both built the same day and described in the next section. First, a per-photo *why* was added to
the base prompt and to the text the review reads, so the question can be asked at all; on its own that moved
the moment review toward memory-shaped moments. Second, the memory question, and a set of other fixed
framings, became selectable **grouping styles**: one trip-wide text-only call per style, memories as sets of
photos with a title, a sentence and why-tags, photos allowed to belong to none. It is a second path beside
the moment path, not a replacement: postcards want moments, recaps and journaling want memories, and the
non-time/location reference sets JO is preparing are the right judge of the memory path.

## Grouping styles: a drop-down of fixed questions

Rather than a free-text prompt editor, the demo now offers a restricted list of **grouping styles** on a finished
run (`llm_pipeline/styles.py`, `GET /api/styles`, `POST /api/runs/{id}/style`). Each is a different fixed
question put to the same per-image text; the photos are never sent again and every style is cached, so
switching costs cents and switching back costs nothing.

| Style | The question | Coverage |
| --- | --- | --- |
| Moments | what happened, in order (the review above) | every photo, one outing at a time |
| Memories | which photos are one memory worth telling, titled by what it was about, why it mattered from awe, excitement, meaningful, calm, fun, connection | trip-wide, photos may be left out |
| Landmark tour | one group per named landmark or point of interest | trip-wide selection |
| Foodie tour | every meal and food or drink stop, titled by what was eaten | trip-wide selection |
| Location highlights | the best few photos of each place, one group per place | trip-wide selection |
| Enjoyable moments | photos that read as fun, calm, awe, connection, excitement or meaningful, by mood | trip-wide selection |

Topic styles share one system prompt with the style's instruction block inserted, answer with groups as frame
sets (`topic-review-1`: frames, title, about, why; a frame in at most one group), and are applied by the same
`ProposalBuilder` as every other proposal, stamped `topic-<style>-1`.

### Change to the base per-image prompt

Checking each style against the eleven collected fields found one gap: Memories and Enjoyable moments need a
per-photo *why*, and the eleven fields carry none (v1 had `emotions`; v2 dropped it with the categories
document). The base prompt was therefore extended by one field, **`why_tags`**: up to three of awe,
excitement, meaningful, calm, fun, connection, the traveler's own vocabulary, read from the photo alone and
described in the prompt as a hint for grouping rather than a fact, empty for practical shots. This is prompt
**v3** and schema **`llm-eval-3`**; it is the only departure from "the eleven fields and no extras" added this
week, and it needs the same explicit decision as the three existing extras. Nothing was re-evaluated: the v1
corpus carries the same tags as `emotions`, and its description line now includes them as `why: ...`.

Because the description line changed, the Moments review was re-run on Shanghai (eight sessions, $0.055) and
the numbers moved: pair F1 0.800 (was 0.818), recall 0.870 (was 0.783), precision 0.741 (was 0.857),
memories split 2 (was 3), merges 2. With the why-tags in view the model reads the hike as one moment (lanterns,
woodland, pavilion, temple, the traveler's own Scene 1) but fuses the traveler's separate temple memory into
it. That is the trade in one line: the why-tags make moments more memory-shaped and less place-shaped, which
raises recall and costs precision. Which way JO wants that dial is a product call, and the drop-down lets them
feel it.

### Measured

Shanghai, Memories style, one trip-wide call of 5.8k tokens, $0.036: 12 memories from 61 photos, 17 photos
left out. Titles and why-tags land close to the traveler's framing: "Woodland Walk to a Temple" (calm, awe,
meaningful), "A Temple Among Modern Towers", "Meeting the Coffee and Retail Robots". Against the 30 reference
memories the grouping is much coarser than the traveler's: pair recall 0.917, precision 0.250, one memory
split, eight proposals each holding more than one reference memory. The model's memories are narrative arcs
of a day ("Learning About Business and Innovation" spans two days and six photos); the traveler's are finer,
fifteen of them a single photo. Two things to read from that: the pair metric over 44 photos punishes coarse
grouping hard, and the non-time/location reference sets JO is preparing are the fair judge of this style. One
clear positive: five of the seven photos the traveler intentionally left out were left unassigned by the
memory view, where the per-image keep signal caught two.

London, Foodie tour ($0.038): 8 stops, "Cocktails, Curry, and Flatbread", "Tavern Beers", "Beer and Fish and
Chips", "Bakery Stop", "Savory Pastry and Hot Drink", with 92 photos not in the view. Landmark tour ($0.068):
35 groups, "Tower Bridge, London" from four vantage points across a day, "Stamford Bridge" x6, "St Paul's
Cathedral, London" x4, "The Great Hall, Winchester", "Selhurst Park", every name taken from the frame text.

## Consequences

- **Two readings of the same photos now exist and disagree by design.** Moments (place-shaped, every photo
  placed, per outing) and the topic styles (memory- or theme-shaped, trip-wide, photos may be left out) are
  stored as separate proposal sets (`regroup-1`, `topic-<style>-1`) beside `group-1` and `refine-1`; nothing
  overwrites anything, and the demo's `groups` is whichever style was last chosen, named in `llm.review.style`.
- **The why-tags trade precision for recall in the moment review.** Shanghai pair F1 0.818 → 0.800, recall
  0.783 → 0.870, precision 0.857 → 0.741; the hike becomes one moment and the temple is fused into it. This is
  the same lever as the time window was in Week 2, now expressed in the prompt text: which way JO wants it is a
  product decision, and the drop-down is the way to feel it rather than argue it.
- **The base per-image prompt is v3 with a field beyond the eleven.** New demo runs collect `why_tags`
  (`llm-eval-3`, same per-image cost); the v1 corpus was not re-evaluated. Task 5's "no extras" now has four
  extras to rule on, not three. If the answer is no, `why_tags` comes out of the prompt and the Memories and
  Enjoyable moments styles lose their per-photo hint and fall back to inferring mood from the descriptions.
- **The memory view is coarser than the traveler's memories.** Twelve arcs against thirty memories on Shanghai
  (recall 0.917, precision 0.250). Read as a first cut of the question, not a tuned answer: the instruction
  block is the knob, the reference sets are the test, and the pair metric punishes coarseness hard. One
  positive is already real: the memory view left five of the seven deliberately excluded photos unassigned,
  where the per-image keep signal caught two.
- **Cost and cache.** Every style is text-only and cached per session under a hash of exactly what the model
  saw, so switching styles on the demo costs cents once and then nothing. The flip side: any change to what the
  model sees (the description line, the grouper's boundaries, a prompt version) re-reviews for cents, which is
  why the moments numbers were re-measured today. Corpus cost this week, all styles and re-runs included, was
  under $0.40.
- **Privacy posture is unchanged.** The review calls send the per-image text, UTC clock times, gaps and metre
  distances; never images, coordinates, file names, counts or identities.
- **Sessions have a ceiling.** Topic styles read the whole trip in one call up to 200 photos, then cut at the
  largest gap between rule-based moments; a 500-photo trip becomes three or four calls whose groups cannot reach
  across the cuts. Acceptable for the sample sets, a known limit for larger trips.

## Notes for the review call

- Task 5 asks for the eleven confirmed fields "only, no extras". The v3 prompt keeps three extras (screenshot
  judgment, keep signal, representative quality) because grouping refinement is built on them, and adds a fourth,
  `why_tags`, for the Memories and Enjoyable moments styles; all four need an explicit decision rather than a
  quiet one.
- Task 7, near-complete field population, is not started.
- Task 10, trip-context prompting, exists as a design (`resources/explainers/set_context.md`), not code.
- The two-variant reference sets arrived 2026-08-27 and the five new sample sets were run and benchmarked the
  same day: see `week4_eval_results.md` for the measured results and `grouping_strategies.md` for the styles
  side by side. Grouping numbers no longer rest on Shanghai alone.

## Reproducing

```bash
uv run python -m jo_pipeline benchmark --dataset "Emiliano_s_Pictures_Shanghai 2026" --llm-version p1-llm-eval-1
uv run python -m jo_pipeline review-moments --dataset "Glenn_London" --llm-version p1-llm-eval-1
```

`benchmark` never calls the API; it reads the cached review records and prints baseline, refined and regrouped
side by side with both keep-signal scores. The artifact lands in `data/runs/<dataset>-v1-benchmark.json` with
every proposal's evidence, including each reviewed moment's title and reason.
