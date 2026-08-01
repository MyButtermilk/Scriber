# AI Plan Generator: Real Estate and Energy

Generate exactly 300 synthetic semantic seed plans conforming to
`contracts/seed_plan_schema.json`.

- Agent id: `generator_real_estate_energy_01`
- Batch id: `real_estate_energy_batch_01`
- Seed range: `300000..300299`
- Domain: `real_estate_energy`
- Output: `data/seeds/generator_real_estate_energy_01/plans.jsonl`
- Manifest: `data/seeds/generator_real_estate_energy_01/manifest.json`

Cover fictional leases, operating costs, defects, handovers, financing,
construction, energy and investment approvals. Include `m²`, `m³`, `€/m²`,
`kW`, `kWh`, `MWh`, `kWh/(m²·a)`, compound units and deliberately ambiguous
unit hard negatives. At least 45% of plans must contain a quantity/unit pair;
at least 20% must exercise a condition, negation or modality. Values and unit
dimensions are immutable. All people, companies, properties and references
must be explicitly fictional.

Set `prompt_hash` to the lowercase SHA-256 of this file, prefixed by
`sha256:`. Validate every JSON object against the schema before completion.
