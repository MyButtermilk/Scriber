# AI Seed Plan Generator: Orthography and Pronouns

Create exactly 300 synthetic semantic seed plans under the closed
`contracts/seed_plan_schema.json` contract.

- Agent id: `generator_orthography_pronouns_01`
- Batch id: `orthography_pronouns_batch_01`
- Seeds: `800000..800299`
- Output: `generator.py`, `plans.jsonl`, `manifest.json`
- `prompt_hash`: SHA-256 of this file's exact bytes, prefixed with `sha256:`

Generate semantic plans only. All content must be fictional and `private_data`
must be false. Use no network, model API, external corpus, target text,
corruption, split, evaluation, or critic verdict.

Produce 150 `general_business` and 150 `hard_negatives` records. Cover
accepted German spelling variants, preferred business-house-form normalization,
already-preferred identity cases, meaning-dependent contrasts, das/dass,
seit/seid, wider/wieder, ss/ß, business capitalization, compounds and hyphens,
and comma rules. Cover formal Sie; capitalized and lowercase personal Du;
Ihr/Euch/Euer; third-person pronouns; ambiguous contrasts; and documents that
combine direct address with third-person references. Include hard negatives
where no normalization is allowed. Each spelling or pronoun surface whose
preservation matters must be explicitly present in a fact's `protected_values`.

At minimum include 100 direct-address-plus-third-person plans, 75 personal-Du
plans, 50 Ihr/Euch/Euer plans, and 100 meaning-dependent or identity hard
negative plans. Plans must be materially varied and schema-valid. The generator
must validate the generated in-memory plans and validate the serialized JSONL
again before it writes a manifest with exact SHA-256 and coverage counts.
