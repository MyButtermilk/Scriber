# Independent AI Target Writer Contract

You are an independent target-writing agent. The semantic plans were generated
by other agents. Do not approve, score, split, corrupt, or train on your own
outputs.

## Inputs you may read

- the explicitly assigned `data/seeds/generator_*/plans.jsonl` files;
- `contracts/de_business_style.yaml`;
- `contracts/sst_v1_schema.json`;
- `src/scriber_polishing/document_ast.py`;
- `src/scriber_polishing/ast_codec.py`;
- `src/scriber_polishing/sst_renderer.py`;
- `src/scriber_polishing/target_renderer.py`;
- `src/scriber_polishing/validators.py`.

Do not read unrelated generated data, evaluation results, model outputs, or
training artifacts.

## Output boundary

Write only inside the explicitly assigned `data/targets/<writer_id>/`
directory:

- `writer.py`: deterministic local writer designed by you;
- `targets.jsonl`: one canonical target record per assigned semantic plan;
- `manifest.json`: provenance, counts, hashes, and deterministic checks.

Do not modify the source plan batches or shared library code.

## Required target behavior

For each plan:

1. Preserve every fact, its order, polarity, modality, condition, protected
   value, entity spelling, number, date, legal citation, and unit.
2. Produce natural, professional text. You may improve the wording of a fact
   only when its complete meaning is unchanged. If uncertain, use the supplied
   fact text verbatim.
3. Follow the requested subject, heading, salutation, paragraph, quote, list,
   closing, signature, attachment, and postscript structure. Never introduce a
   subject, heading, quote, attachment, or postscript when the plan forbids or
   omits it. Optional `has_quote`, `has_attachments`, and `has_post_script`
   fields are authoritative when present.
4. Derive structural labels only from `document_type`,
   `semantic_plan.structure.paragraph_topics`, or explicit facts. Do not invent
   a new factual claim. Structural labels must be natural reader-facing text:
   never expose snake_case identifiers, raw enum values, code-like tokens, or
   a mechanical `Betreff: <document_type>` placeholder. A subject should name
   the first supplied topic and, when useful, one supplied entity; the SST
   `SUBJECT` block already carries its structural meaning.
5. Use only fictional entities already present in the plan.
6. Keep German address pronouns consistent with `address_mode`; for `en` and
   `mixed`, preserve the intended language mixture.
7. Build the closed `Document` AST first. Derive SST, plain text, Markdown, and
   safe HTML only through the repository renderers.
8. Set `source_text` to the uncorrupted canonical plain-text target. It is an
   identity source at this stage.
9. Flatten `protected_spans` from fact `protected_values`, preserving first
   occurrence order. Every listed span must occur in the target.
10. Set provenance from the plan. `semantic_plan_sha256` is `sha256:` plus the
    SHA-256 of canonical compact JSON (`ensure_ascii=False`, `sort_keys=True`,
    separators `(",", ":")`) for the `semantic_plan` object.
11. Set `target_prompt_hash` to `sha256:` plus the SHA-256 of this handoff
    file's exact bytes.
12. Set `normalize_valid_variants` to `false`. Use empty
    `applied_normalizations` and `rejected_normalizations`. Populate
    `ambiguity_flags` only from applicable plan risk tags containing
    `ambiguous`, `ambiguity`, or `hard_negative`.

Every record must pass `validate_canonical_record`, including strict
AST/SST/renderer equivalence. IDs and source seeds must be unique.

## Manifest

Include at least:

- `manifest_version: 1`;
- writer id and prompt hash;
- assigned input batches and their exact SHA-256;
- record count and unique-id counts;
- `targets_sha256`;
- counts by domain, language, document type, address mode, block type, list
  kind, fact count, and protected-span presence;
- schema validation count;
- AST-SST-AST round-trip count;
- renderer consistency count;
- confirmation that no critic verdict, split, corruption, model output,
  private data, or external API call is present.

Serialize JSONL as UTF-8, compact sorted JSON with one final newline. A second
run must reproduce `targets.jsonl` byte for byte.
