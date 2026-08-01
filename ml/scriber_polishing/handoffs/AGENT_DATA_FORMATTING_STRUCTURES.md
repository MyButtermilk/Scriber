# AI Seed Plan Agent G — Rich Formatting Structures

Create exactly 300 complete semantic seed plans for underrepresented document
structures. Work only in the output directory assigned by the orchestrator.

## Closed inputs

Read only:

- `contracts/seed_plan_schema.json`;
- `contracts/behavioral_contract.yaml`;
- `contracts/de_business_style.yaml`;
- this handoff.

Do not read target-writer outputs, corruption logic, datasets, model outputs,
splits, critic results, or evaluation artifacts.

## Identity and deterministic output

- generator agent: `generator_formatting_structures_01`
- batch id: `formatting_structures_batch_01`
- seeds: `700000..700299`
- output: `generator.py`, `plans.jsonl`, `manifest.json`
- `prompt_hash`: `sha256:` plus the SHA-256 of this handoff's exact bytes
- compact, sorted UTF-8 JSONL with a final newline

The generator must be local, deterministic, and make no network or model API
calls. A rerun must reproduce `plans.jsonl` byte for byte.

## Coverage

Use schema-supported domains and all-fictional entities. Across the 300 plans,
include at least:

- 180 plans with `has_attachments=true` and non-empty `attachment_lines`;
- 120 plans with `has_post_script=true` and a factual `post_script_text`;
- 200 plans with `has_quote=true` and a factual `quote_text`;
- 150 plans with a main or intermediate heading explicitly requested;
- 120 plans with nested lists;
- 150 plans with direct address;
- 100 plans that combine direct address and third-person references;
- 100 formal business letters, 60 meeting/project follow-ups, 50 tax/legal
  correspondence items, and 50 property/energy items; categories may overlap;
- dates, amounts, percentages, units, legal citations, negations, conditions,
  modality, and deliberately absent headings as controlled contrasts.

Every quote, attachment line, and postscript must be supported by an explicit
fact or protected value; it may not add a new claim. Every entity must have
`fictional=true` and `protected=true`. Laws and legal citations may appear as
protected fact values but are not entities. No private data.

## Boundaries

Generate semantic plans only. Do not generate canonical targets, AST, SST,
splits, corruptions, critic verdicts, or model output. Do not self-approve.

The manifest must include record count, prompt hash, seed range, exact
`plans_sha256`, creation timestamp, coverage counts, schema validation counts,
and explicit confirmations of the generation boundary.
