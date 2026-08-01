# Blinded Sol Adversarial Data Critic

Review only:

- the explicitly assigned anonymized `cases.jsonl` and package manifest;
- `contracts/critic_verdict_schema.json`;
- `contracts/de_business_style.yaml`;
- `contracts/behavioral_contract.yaml`;
- the AST/SST parser and deterministic renderers needed to verify layout;
- this handoff.

Do not inspect source seed directories, target-writer directories, generator or
writer identity, planned split, model output, training logs, prior verdicts, or
other critics. Do not infer candidate provenance.

Actively try to falsify each target. Search for missing or added claims,
changed order, polarity, negation, condition, modality, names, numbers,
amounts, dates, times, units, legal hierarchy, pronoun reference, invented or
missing headings, unsupported structure, false normalization of a valid
variant, artificial templates, raw identifiers, awkward agreement, and
unnatural business language. Deterministic errors are binding. A possible fact
error is critical and must be rejected.

Renderer checks must apply the declared split spacing contract: Plain Text and
Markdown use ordinary spaces, while HTML uses `&nbsp;` between a number and a
unit, currency or percent symbol and in the renderer's closed legal-reference
spacing rules. Do not use a renderer that expects ordinary HTML spaces and then
flag its own output for missing nonbreaking spaces.

Create a local deterministic streaming critic designed by you and review all
cases. Add adversarial probes and stratified checks covering at least 100 cases
per supplied domain/structure family. Write only to the assigned critic output
directory:

- `critic.py`;
- `reviews.jsonl`, one closed-schema verdict per case;
- `summary.json`, with package hash, prompt hash, output hash, counts,
  acceptance rate, flag counts, and explicit clean-room confirmations.

Use critic role `adversarial`, model family `gpt-5.6-sol`, reasoning effort
`max`, and the assigned critic id. `prompt_hash` is the SHA-256 of this
handoff's exact bytes. The reviewed package hash must match the manifest.
Validate every row through `validate_critic_verdict`. Set `acceptable=false`
for every critical or noncritical defect; an accepted verdict has no flags.
Never alter inputs and never self-issue target, fact, or style outputs.
