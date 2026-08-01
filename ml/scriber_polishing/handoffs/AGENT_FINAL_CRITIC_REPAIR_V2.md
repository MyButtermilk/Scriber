# Final critic repair v2.01

Build one deterministic post-review repair layer from:

- `data/targets/target_repair_adversarial_v5_02`
- `data/targets/target_repair_coverage_v1_01`
- the final blinded style and two final blinded adversarial review streams
- `artifacts/critic_package_final_v1` and its private case-to-seed index

The layer must emit exactly 2,665 plans and 2,665 canonical targets. It may
repair only genuine, closed findings present in the final review streams:

- artificial English record-reference wording
- artificial meeting/value, registration, and ticket wording
- unsupported `Inhalt bleibt unverändert.` blocks
- redundant release, review, and completed-review condition wording
- the three closed grammar/case patterns
- the three closed lowercase headings
- the closed operating-temperature, general, real-estate-list, and tax article
  omissions
- the closed mixed-language compound pair
- the declared code-switch instruction that was translated into German
- the closed artificial causal noun

Preserve every protected surface, entity, fact identity, fact order, polarity,
modality, condition, and document block/list order. Plan text and the two
reviewed structure labels may change only where the closed repair requires it.
Regenerate SST, plain text, Markdown, and HTML from the repaired canonical AST.
Validate seven deterministic variants for every target.

Do not use a human curator, external corpus, generative API, training data,
evaluation data, split data, or model output. Do not repair calibrated false
positives involving conservative list labels, canonical postscript labels, the
six unrelated orthopron condition sentences, or the protected lowercase
headings `umbauter Raum` and `lichte Höhe`.

Build twice, compare every emitted byte and metadata object, stage all payloads
in a temporary sibling directory, validate the staged batch, and replace only
the five named output artifacts atomically.
