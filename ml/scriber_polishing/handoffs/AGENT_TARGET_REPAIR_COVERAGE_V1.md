# Coverage-recovery repair v1.01

Build one immutable, deterministic repair overlay for exactly the forty records
from `generator_coverage_recovery_02` and
`target_writer_coverage_recovery_02`. Read only the assigned seed/target
packages, the pinned blinded preflight package and its private index, the local
canonical AST/SST/rendering contracts, and this handoff.

The closed preflight scope contains fourteen rejected cases: seven formal
address conflicts, ten duplicate subject-and-heading findings, and three cases
in both groups. Do not infer extra repairs from similar-looking records.

- For each approved formal-address repair, set the repaired plan's
  `address_mode` to `personal_du_capitalized`, and change the exact fact
  surface from `Seit Montag seid ihr ...` to `Seit Montag seid Ihr ...`.
  Keep `Seit` and `seid` exactly as written; only the direct-address pronoun
  uses the capitalized `Ihr` form.
- For each approved duplicate-heading repair, keep the subject, remove the
  redundant heading block, and set `semantic_plan.structure.heading_levels`
  to an empty list.
- Preserve all unrelated plan fields, entities, fact order, protected values,
  list structure, quote/closing/signature material, and all non-repaired AST
  blocks. Regenerate SST, plain text, Markdown, and HTML only from the repaired
  canonical AST.
- Validate every repaired plan and target, validate all seven canonical
  expansions for every target, prove exact scope and deterministic double-run
  bytes, and fail closed on hash, mapping, schema, renderer, or scope drift.

Do not edit either source package. Do not inspect a training, validation, test,
challenge, or AI-Gold split. Do not call a generative API or use an external
corpus. Write only the new repair package.
