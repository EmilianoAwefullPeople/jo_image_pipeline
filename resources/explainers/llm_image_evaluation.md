# LLM Image Evaluation

Standalone subsystem (`llm_pipeline/`) that evaluates trip photos through a visual LLM (`openai/gpt-5.6-sol` via OpenRouter) for signals a CV pipeline cannot produce. It shares nothing with `jo_pipeline` beyond `.env` and the dataset folders; it never writes to the sqlite database.

## What it evaluates

One structured call per image returns: a one-sentence caption, scene classification, activity interpretation, a landmark candidate (hypothesis, never a fact), meaningful visible text, screenshot/document judgment with travel relevance (a plane ticket is relevant media, not something to drop), emotional salience using the traveler's own vocabulary (awe, excitement, meaningful, calm, fun, connection), a keep/leave-out/unsure memory signal (exclusion is a product concept the rule-based pipeline does not model), a representative-quality score based on composition and story rather than sharpness or file size, and a suggested journaling prompt.

## Trust and privacy rules

- Null is a valid outcome; the model is instructed never to invent, and every inferred value carries confidence and evidence.
- No people counting and no identity claims: the schema has no fields for them and the prompt forbids them in free text (both are gated on privacy review per the reliability matrix).
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

Requires `JO_OPENROUTER_API_KEY` in `.env`. Per the sprint terms, no image may be sent to a third-party AI service without Glenn and Emiliano's written approval; `run` is the only command that transmits anything.
