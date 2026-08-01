# AI Plan Generator: Technical and Project Communication

Generate exactly 300 synthetic semantic seed plans conforming to
`contracts/seed_plan_schema.json`.

- Agent id: `generator_technical_project_01`
- Batch id: `technical_project_batch_01`
- Seed range: `400000..400299`
- Domain: `technical_project`
- Output: `data/seeds/generator_technical_project_01/plans.jsonl`
- Manifest: `data/seeds/generator_technical_project_01/manifest.json`

Cover fictional software releases, incident follow-ups, requirements, project
status, architecture decisions, delivery plans, test reports and vendor
coordination. Protect version numbers, product names, code fragments, file
paths, IDs, URLs and technical units. Include both legitimate ordered task
lists and prose that must not become a list. At least 25% of plans contain
English technical terms in otherwise German text; at least 20% contain a
negation, condition or modality. Use no real credentials, repositories,
incidents, customer data, or private Scriber records.

Set `prompt_hash` to the lowercase SHA-256 of this file, prefixed by
`sha256:`. Validate every JSON object against the schema before completion.
