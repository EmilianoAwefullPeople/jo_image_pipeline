# LLM Image Evaluation

Standalone subsystem (`llm_pipeline/`) that evaluates trip photos through a visual LLM (`openai/gpt-5.6-sol` via OpenRouter) for signals a CV pipeline cannot produce. It shares nothing with `jo_pipeline` beyond `.env` and the dataset folders; it never writes to the sqlite database.

## What it evaluates

The field set is fixed by `resources/LLM_Data-Extraction_Categories.pdf`. Every category in that document has to pass two standards: it carries context no deterministic pipeline step can produce, and its value is directly usable in the graphic output (postcards, recap sequencing, template and crop choice). One structured call per image returns all eleven:

| # | Field | Shape |
| --- | --- | --- |
| 1 | `general_description` | 12 to 17 words, holistic, readable on its own |
| 2 | `scene_setting` | Multi-select closed list plus `other_detail` |
| 3 | `landmark` | Name, `high`/`medium`/`low` tier, evidence, all null together when nothing is identifiable |
| 4 | `notable_subjects` | Up to 4 non-human subjects as "Category, specific name" |
| 5 | `focal_points` | Multi-select; carries the only permitted single/couple/group distinction |
| 6 | `activity` | Multi-select closed list plus `other_detail`, empty when none is depicted |
| 7 | `environment` | General multi-select plus an optional specific style phrase |
| 8 | `composition` | Multi-select framing, distinct from the pipeline's own blur and quality signals |
| 9 | `weather` | Multi-select visual read, cross-checks the fetched weather API |
| 10 | `keyword_tags` | 3 to 6 short tags |
| 11 | `photographic_style` | Multi-select treatment, distinct from OpenCV's raw colour palette |

Three further fields are pipeline decision signals rather than extraction categories, and the grouping stage is built on them: screenshot/document judgment with travel relevance (a plane ticket is relevant media, not something to drop), a keep/leave-out/unsure memory signal (exclusion is a product concept the rule-based pipeline does not model), and a representative-quality score based on composition and story rather than sharpness or file size.

Closed lists are closed. `other` is only valid alongside an `other_detail` naming what it was, so a closed list never degrades into free text. A response that misses the bounds (word count, tag count, subject count, an unexplained `other`) fails validation and is retried once with the error before it is stored for review.

## Reviewing the output

The web demo renders every field against its image in the Extracted fields panel, in the order of the categories document, showing values exactly as the model returned them. Records that failed validation appear there too, with the failure detail, so a bad response is visible rather than silently missing.

## Trust and privacy rules

- Null is a valid outcome; the model is instructed never to invent, and every inferred value carries confidence and evidence.
- No headcounts and no identity claims: the schema has no fields for them and the prompt forbids them in free text (both are gated on privacy review per the reliability matrix). `focal_points` carries the single/couple/group distinction the template choice needs, as a closed category rather than a number.
- Only a downscaled clean re-encode leaves the machine (1024px max edge JPEG, no EXIF, no GPS). Capture time is the only metadata sent, as text context.
- Fixed model routing with `allow_fallbacks: false` and `data_collection: "deny"`. A failed call is a visible failure, never a hidden substitution.
- Invalid responses are retried once with the validation error, then stored for review. Outputs are provisional detected metadata and never override traveler decisions.

## Cost controls

Every record stores tokens, cost, latency, attempts, and model/prompt/schema versions. Records are immutable per-image JSON under `data/llm_runs/<dataset>/p<prompt_version>-<schema_version>/`, keyed by content hash, so interrupted runs resume without re-spending. `estimate` prices a run without any network call; `--limit` bounds a run.

## Commands

```bash
uv run python -m llm_pipeline estimate --dataset "Dan Egypt 2024"
uv run python -m llm_pipeline run --dataset "Dan Egypt 2024" --limit 5
```

Requires `JO_OPENROUTER_API_KEY` in `.env`. `run` is the only command that transmits anything.
