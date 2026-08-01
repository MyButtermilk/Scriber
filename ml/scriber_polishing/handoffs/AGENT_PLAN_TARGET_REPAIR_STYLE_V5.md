# AI style-plan and target repair

Use only the immutable post-v4.02 plans and targets, the blinded v5 style
verdicts with their private case index, and the declared rendering and style
contracts.

Apply the following closed repairs:

- for every `html_nbsp_contract_violation`, keep the AST and all textual
  projections unchanged and regenerate HTML through the corrected deterministic
  renderer;
- for every `mechanical_condition_template`, preserve the fact, modality,
  negation, protected values, order, and the semantic plan byte-for-byte; use
  the independently audited closed condition-equivalence families to prove that
  the unchanged condition is already naturally realized by the fact, and only
  then remove the appended sentence beginning exactly with `Die Regelung gilt
  unter folgender Bedingung:`;
- regenerate SST, Plain Text, Markdown, and HTML from the validated AST;
- reject unknown flags, unrecognized condition paraphrases, missing source
  records, hash mismatches, schema failures, renderer mismatches, or changed
  scope.

Do not inspect or use a training, validation, test, challenge, or AI-Gold split.
Do not add facts, headings, entities, or external text. Do not call any
generative API. Serialize deterministically and validate all seven canonical
expansions for every output target.
