# Wrap-up Q&A

Answers to the wrap-up questions, as of 2026-08-30. Everything here is sourced from the code and the committed eval artifacts; file references point at the current state of `main`.

## 1. What is the criteria and logic behind "keep signal" and "representative quality"?

Both are per-image judgments made by the vision model during evaluation, each defined by one sentence of prompt criteria. Neither is a heuristic: there are no thresholds, weights, or derived rules behind the values themselves.

### Keep signal

The prompt (`llm_pipeline/prompts/system_v3.txt:30`) instructs:

> memory: judge whether this photo belongs in a kept travel memory. keep for photos with story or emotional value, leave_out for accidental shots, redundant frames, or content with no memory value, unsure when it depends on context you cannot see. reason states the judgment in one sentence.

The schema (`llm_pipeline/schema.py`) constrains the answer to `keep` / `leave_out` / `unsure`, plus a free-text reason and a 0-1 confidence.

There is a second producer: the set-level moment review can also suggest leave-outs (`llm_pipeline/prompts/review_system_v1.txt:11`), restricted to frames that "add nothing to any moment" (a practical screenshot, an accidental shot, a near repeat of an adjacent frame) and explicitly forbidden from excluding a frame because of who or what it shows. A per-image `leave_out` outranks a review `leave_out` for provenance (`jo_pipeline/regroup.py:295-299`).

Consumption is deliberately simple. Refinement (`jo_pipeline/refine.py:128-144`) drops a photo from a group proposal only on the exact value `leave_out` — `unsure` and `keep` are both retained, and confidence is recorded as evidence but never compared against anything. Drops are reversible and visible in the demo ("left out" / "left out by review" badges; the photos stay available).

Measured behaviour: precise but insensitive. Week 3 measured recall 0.29 against the traveler's own exclusions with a 0.00 false-positive rate (`resources/explainers/week3_findings.md`). Week 4 per set: Berlin recall 1.00 / FPR 0.029, Costa Rica 0.25 / 0.00, Zurich 0.25 only with the moment review (`resources/explainers/week4_eval_results.md`).

### Representative quality

The prompt (`llm_pipeline/prompts/system_v3.txt:31`) instructs:

> representative_quality: score 0 to 1 how well this photo could represent its moment to the traveler, based on composition, story, and expressiveness. Do not reward or penalize sharpness, resolution, or technical file quality.

The score is consumed two ways in refinement (`jo_pipeline/refine.py`):

- A plain argmax over group members elects the group's representative photo, replacing the baseline rule of "sharpest member by Laplacian blur score".
- The mean of member scores contributes 30% of the refined group score: `0.4 * cohesion + 0.3 * location_support + 0.3 * model_quality`.

## 2. What is the LLM output length limit per field?

There is no `max_tokens` on the API call (`llm_pipeline/client.py`). Length is controlled entirely by prompt wording plus pydantic validation, with a single retry when validation fails.

| Field | Limit | Enforcement |
|---|---|---|
| `general_description` | 12 to 17 words | hard-validated in code (`llm_pipeline/schema.py:185-190`) |
| `notable_subjects` | max 4 items | pydantic `max_length=4` |
| `keyword_tags` | 3 to 6 "short tags or phrases" | pydantic 3-6 items; no per-item character limit |
| `why_tags` | max 3, from a closed list of 6 | pydantic plus enum |
| Moment and group titles | at most 8 words | hard-validated (`llm_pipeline/schema.py:203-205, 254-256`) |
| `memory.reason`, review and group `reason` / `about` | "one sentence" | prompt wording only |
| `environment.specific_style` | "a short descriptive phrase" | prompt wording only |
| `landmark.name` / `.evidence`, `representative_quality.reasoning`, `*_detail` fields | none | free text |

Two caveats. The count bounds are stripped from the JSON schema sent to OpenRouter (strict structured output does not accept them), so the item limits are enforced only after the fact by pydantic with one retry; in practice 149 of 149 week 4 responses were schema-valid. And nothing is ever truncated after the fact — model output reaches storage and the grouping prompts verbatim.

## 3. What does the landmark detection ceiling look like as of now?

What is measured is population, not accuracy: the fraction of images where the model produced a landmark name at all. Nothing yet scores whether a produced name is correct.

On the five reference sets (prompt v3, `openai/gpt-5.6-sol`), from `resources/explainers/week4_eval_results.md`:

| Set | Landmark populated | Rate |
|---|---|---|
| Acapulco | 0 / 29 | 0% |
| Berlin | 1 / 36 | 3% |
| Costa Rica | 1 / 35 | 3% |
| Nazare | 6 / 19 | 32% |
| Zurich | 5 / 30 | 17% |
| All five | 13 / 149 | ~9% |

The ceiling is trip-shape-dependent rather than model-dependent: the older dense-sightseeing sets ran far higher on the same metric (Glenn_London 58% with 35 distinct named landmark groups, Cruise 50%, Egypt and Shanghai 36%). Acapulco's zero is judged correct — a resort trip with no named sights. Berlin is the clear miss case: recognisable streetscapes stayed null.

Landmark is the pipeline's only weak extraction field — 9 of the 11 confirmed fields populate at 100% across all 149 images. The gap is a deliberate trade: the prompt's never-invent trust rules buy zero fabricated names at the cost of recall. The landmark tour grouping style inherits every miss (`resources/explainers/grouping_strategies.md`). The designed-but-unbuilt fix is the per-set context brief in `resources/explainers/set_context.md`, which injects a survey of the whole set as a prior without raising landmark confidence tiers.
