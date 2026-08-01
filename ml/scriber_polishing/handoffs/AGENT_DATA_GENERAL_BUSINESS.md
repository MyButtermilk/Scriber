# AI Plan Generator: General Business

Generate exactly 300 synthetic semantic seed plans conforming to
`contracts/seed_plan_schema.json`.

- Agent id: `generator_general_business_01`
- Batch id: `general_business_batch_01`
- Seed range: `100000..100299`
- Domain: `general_business`
- Output: `data/seeds/generator_general_business_01/plans.jsonl`
- Manifest: `data/seeds/generator_general_business_01/manifest.json`

Cover formal letters, business email, offers, invoices, payment reminders,
deadlines, appointments, suppliers, customers, contracts, complaints,
handover, budgets, projects and banking. Use varied fact counts, sentence
structures, address modes, paragraphs and justified lists. At least 20% must
contain a protected number/date/time/amount/percentage; at least 20% must
contain a negation, condition, or non-factual modality. Entities are explicitly
fictional. Do not write target text or corrupted transcripts. Do not use
private Scriber data, external generative APIs, real case numbers, or real
personal records.

Set `prompt_hash` to the lowercase SHA-256 of this file, prefixed by
`sha256:`. Validate every JSON object against the schema before completion.
