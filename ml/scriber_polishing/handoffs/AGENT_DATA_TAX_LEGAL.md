# AI Plan Generator: Tax and Legal Business

Generate exactly 300 synthetic semantic seed plans conforming to
`contracts/seed_plan_schema.json`.

- Agent id: `generator_tax_legal_01`
- Batch id: `tax_legal_batch_01`
- Seed range: `200000..200299`
- Domain: `tax_legal`
- Output: `data/seeds/generator_tax_legal_01/plans.jsonl`
- Manifest: `data/seeds/generator_tax_legal_01/manifest.json`

Cover fictional tax-office, tax-adviser, audit, objection, deadline,
assessment, VAT, trade-tax and corporate-tax correspondence. Use only
officially verified law abbreviations from `contracts/source_registry.md`.
Include diverse single/multiple citations, hierarchy levels, amounts,
percentages, dates, conditions, negations and modalities. At least 35% of plans
must contain a legal citation or a legal hard negative. Never invent a law
abbreviation, provide legal advice, or use a real non-public case, person, tax
number, file number, or private record.

Set `prompt_hash` to the lowercase SHA-256 of this file, prefixed by
`sha256:`. Validate every JSON object against the schema before completion.
