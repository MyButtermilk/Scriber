# AI Seed Plan Generator: Coverage Completion

Generate exactly 300 synthetic, German semantic seed plans under the closed
`contracts/seed_plan_schema.json` contract.  This batch contains plans only:
no targets, corruptions, splits, evaluations, approvals, or critic verdicts.

- Agent id: `generator_coverage_completion_01`
- Batch id: `coverage_completion_batch_01`
- Seeds: `900000..900299`
- Outputs: `generator.py`, `plans.jsonl`, `manifest.json`, `report.json`
- `prompt_hash`: SHA-256 of this file's exact bytes, prefixed with `sha256:`

All text is fictional, business-quality, and generated locally without a
model or network API.  Every protected value must occur verbatim in the fact
that owns it.  The records deliberately close these coverage gaps:

1. 100 positive real-estate/energy records with `m²` or `m³`, `€/m²`,
   `kWh/(m²·a)`, plus one of `m³/h`, `km/h`, `m/s`, or `Mbit/s`.
2. 50 hard-negative ambiguous-unit records containing `m zwei`, `18 K`, a
   bare `pro Jahr`, model names, or internal identifiers, with no safely
   normalizable unit-symbol fact.
3. 50 dense legal records with protected `Artikel 3 Absatz 1 GG`, a
   paragraph-plus-sentence citation, `§ 8 Nummer 1 Buchstabe a GewStG`, and
   `§§ 8c und 8d KStG`.
4. 50 direct-address orthography/pronoun contrast records covering
   Ihr/Euch/Euer, third-person reference, and a protected spelling contrast;
   at least half are meaning-dependent hard negatives with no normalization.
5. 50 long four-theme business/format records with four facts and four
   paragraph topics, an explicit no-heading constraint, and hard negatives
   for ordinary format words such as Überschrift, Absatz, Punkt, or Betreff.

The deterministic generator validates the in-memory records and serialized
JSONL, all exact quotas, uniqueness, private-data exclusion, protected-value
ownership, SHA-256 stability, and byte-identical regeneration.
