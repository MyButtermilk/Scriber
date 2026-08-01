# Blinded Style Critic

Review only:

- the explicitly assigned anonymized `cases.jsonl` and package manifest;
- `contracts/critic_verdict_schema.json`;
- `contracts/de_business_style.yaml`;
- `contracts/behavioral_contract.yaml`;
- the AST/SST parser and deterministic renderers needed to verify layout;
- this handoff.

Do not inspect source seed directories, target-writer directories, generator or
writer identity, planned split, model output, training logs, prior verdicts, or
other critics.

Check spelling, grammar, punctuation, natural business language, English and
mixed-language naturalness, address pronouns, headings, subjects, paragraphs,
blank lines, greetings, closings, signatures, quotes, attachments, postscripts,
and list justification. Reject raw enum/snake_case labels, mechanical
templates, awkward agreement, artificial phrases, unsupported structural
labels, and address-mode conflicts. Never overrule deterministic failures or
accept a possible fact change.

Apply the declared split spacing contract: Plain Text and Markdown use
ordinary spaces, while HTML uses `&nbsp;` between a number and a unit,
currency or percent symbol and in the renderer's closed legal-reference
spacing rules.

Create a local deterministic streaming critic designed by you and review all
cases. In addition, derive and exercise stratified checks covering at least 100
cases per supplied domain/structure family. Write only to the assigned critic
output directory:

- `critic.py`;
- `reviews.jsonl`, one closed-schema verdict per case;
- `summary.json`, with package hash, prompt hash, output hash, counts,
  acceptance rate, flag counts, and explicit clean-room confirmations.

Use critic role `style`, model family `gpt-5.6-terra`, reasoning effort `max`,
and the assigned critic id. `prompt_hash` is the SHA-256 of this handoff's exact
bytes. The reviewed package hash must match the manifest. Validate every row
through `validate_critic_verdict`. Set `acceptable=false` for every critical or
noncritical defect; an accepted verdict has no flags. Never alter input cases
and never self-issue a target or fact verdict.
