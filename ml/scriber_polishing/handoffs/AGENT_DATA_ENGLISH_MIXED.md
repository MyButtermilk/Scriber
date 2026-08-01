# AI Plan Generator: English and Mixed-Language Business

Generate exactly 300 synthetic semantic seed plans conforming to
`contracts/seed_plan_schema.json`.

- Agent id: `generator_english_mixed_01`
- Batch id: `english_mixed_batch_01`
- Seed range: `600000..600299`
- Domain: `english_mixed`
- Output: `data/seeds/generator_english_mixed_01/plans.jsonl`
- Manifest: `data/seeds/generator_english_mixed_01/manifest.json`

Cover English business messages and German correspondence with natural English
product, software, finance and project terminology. The output language must
remain the input language. Protect names, versions, IDs, amounts, dates,
URLs and technical terms. Include identity cases and code-switching hard
negatives. All entities and scenarios are fictional; no private Scriber data,
real credentials, or external generative APIs may be used.

Set `prompt_hash` to the lowercase SHA-256 of this file, prefixed by
`sha256:`. Validate every JSON object against the schema before completion.
