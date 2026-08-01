# AI Plan Generator: Hard Negatives and Safety

Generate exactly 300 synthetic semantic seed plans conforming to
`contracts/seed_plan_schema.json`.

- Agent id: `generator_hard_negatives_01`
- Batch id: `hard_negatives_batch_01`
- Seed range: `500000..500299`
- Domain: `hard_negatives`
- Output: `data/seeds/generator_hard_negatives_01/plans.jsonl`
- Manifest: `data/seeds/generator_hard_negatives_01/manifest.json`

Target meaning-preservation hazards: negations, double negations, conditions,
possibility versus certainty, recommendation versus obligation, close numbers,
amounts, dates, times, names, legal hierarchy, intentional repetition,
questions that must not be answered, and words such as Absatz, Punkt,
Überschrift, Betreff, Liste, Signatur or fett used as ordinary content. At
least 80% of plans contain one critical risk tag and at least 40% contain two
or more. Do not provide target text, answers to questions, real identifiers,
private records, or invented legal abbreviations.

Set `prompt_hash` to the lowercase SHA-256 of this file, prefixed by
`sha256:`. Validate every JSON object against the schema before completion.
