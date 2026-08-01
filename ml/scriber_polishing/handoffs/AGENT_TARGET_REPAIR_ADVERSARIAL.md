# Adversarial target-repair contract

Apply only findings issued by the pinned blinded AI-critic packages.

- Reject every semantic plan previously marked `pronoun_referent_error`.
  Do not infer a referent, replace a protected pronoun with a name, or add an
  explanatory sentence.
- Preserve every fact, condition, modality, polarity, protected value, fact
  order, list item, and declared document-structure family.
- Repair `von Finanzamt <Name>` to `vom Finanzamt <Name>`.
- Correct `Liebe`/`Lieber` agreement for the generated named recipients.
- Restore a natural `sie` or `er` only where a target writer duplicated the
  same named subject in `X teilte mit, dass X`; the underlying semantic plan
  remains authoritative.
- Replace a duplicated subject/heading surface only with a distinct
  `paragraph_topics` value already declared by the semantic plan.
- Preserve the exact protected English terms `idempotency key`,
  `canary deployment`, and `feature flag`; repair only their German
  grammatical scaffolding.
- Plain text and Markdown use ordinary spaces. Safe HTML uses `&nbsp;`
  between numbers and units, currency or percent symbols, and inside the
  renderer's closed legal-reference spacing rules.
- Rebuild SST, AST projections, plain text, Markdown, and safe HTML
  deterministically, then fail closed on any validation or expansion error.
- No human curation, external corpus, generative API, model output, training
  split, test split, or challenge split may be read.
