# Structured pronoun-plan and target repair contract

This repair consumes only hash-pinned AI-critic verdicts, their private
case-to-seed index, the source semantic plans, the current canonical targets,
and local contracts/code. It must not read training, validation, test,
challenge, model-output, private Scriber, external-corpus, or human-curation
data.

For every plan previously marked `pronoun_referent_error`:

- the named actor is already explicit immediately before the contradictory
  generated `er` or `sie`;
- promote that same declared actor into the affected action clause;
- remove a displaced pronoun from `protected_values` only when its exact
  surface no longer occurs in the repaired fact;
- preserve all other protected values, facts, order, polarity, conditions,
  modality, entities, risk tags, and structure;
- set the plan generator/batch identity to this repair layer and hash this
  handoff as its prompt;
- remove the unsupported sentence
  `Das Pronomen „…“ bezieht sich dabei auf ….` from the target;
- never add a metalinguistic explanation or infer an actor not already named
  in the source fact.

Then apply the closed v4 style repairs, rebuild every projection, recompute the
semantic-plan hash, and verify every plan, target, protected span, SST
round-trip, renderer, and seven-variant canonical expansion twice.

Plain Text and Markdown retain ordinary spaces. Safe HTML uses `&nbsp;`
between numbers and units, currency or percent symbols and in the renderer's
closed legal-reference spacing rules.

Serialization is UTF-8, compact sorted JSONL, LF-only, seed-sorted, timestamp
free, and byte-deterministic. No generative API or human decision is allowed.
