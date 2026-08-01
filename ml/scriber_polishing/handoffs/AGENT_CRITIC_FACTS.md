# Blinded Fact Critic

Review only:

- the explicitly assigned anonymized `cases.jsonl` and package manifest;
- `contracts/critic_verdict_schema.json`;
- `contracts/behavioral_contract.yaml`;
- the AST/SST parser and deterministic renderers needed to verify structure;
- this handoff.

Do not inspect source seed directories, target-writer directories, generator or
writer identity, planned split, model output, training logs, prior verdicts, or
other critics.

Re-extract every supplied fact and compare order, polarity, condition,
modality, name, number, amount, percentage, date, time, unit, technical term,
legal hierarchy, paragraph order, quote, list, attachment, and postscript
structure. Every protected value must survive. A possible material mismatch is
a critical rejection, never a style tie.

Create a local deterministic streaming critic designed by you and review all
cases. In addition, derive and exercise stratified checks covering at least 100
cases per supplied domain/structure family. Write only to the assigned critic
output directory:

- `critic.py`;
- `reviews.jsonl`, one closed-schema verdict per case;
- `summary.json`, with package hash, prompt hash, output hash, counts,
  acceptance rate, flag counts, and explicit clean-room confirmations.

Use critic role `fact`, model family `gpt-5.6-terra`, reasoning effort `max`,
and the assigned critic id. `prompt_hash` is the SHA-256 of this handoff's exact
bytes. The reviewed package hash must match the manifest. Validate every row
through `validate_critic_verdict`. Never alter input cases and never self-issue
a target or style verdict.
