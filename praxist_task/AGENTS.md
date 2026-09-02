# PRAXIST LFM-only sandbox

## Active scope

The sole active model path is `LiquidAI/LFM2.5-350M-Base`. Boldt and the
Short-only recovery pipeline are retired. Their artifacts remain historical
engineering and regression evidence only; they are not training, evaluation,
export, or product inputs for the active winner.

The accepted Development recipe is the terminal PRAXIST Mixed-Rehearsal run
`run_2026-09-02_03-21-22-965067_fresh_windows_lfm2_350m_mixed_rehearsal`.
It completed 6,400 deterministic microsteps and 400 fresh AdamW optimizer
steps, then evaluated one transient BF16 teacher on the bound 200 Long plus
400 Short recipe-regression cases. The immutable automated result remains a
near-pass, not a hard-gate pass: Long exact was 197/200, Short exact was
399/400, and Long structure was 0.9998125 against the fixed 1.0 floor.

On 2026-09-02 the repository owner explicitly accepted that measured precision
and declared the Development gate met. Record this only as a separate,
create-once owner-acceptance receipt bound to the exact run, metrics, and four
residual cases. Never change the automated gate fields, thresholds, source
summary, or findings. The Development adapter was correctly deleted and is
recipe evidence only; it must never be reconstructed or used as a Production
parent.

The sole active operator is `fresh_windows_lfm2_350m_mixed_production`. The
historical All-2,000 hash proved irreproducible in one bounded native-Windows
retry. The active lineage therefore resumes the byte-verified fresh Mithril
All-2,000 LoRA directory SHA-256
`cc29a35b0cb680c333abfa9028786a82381c0e642722b9bd4e3f7550e55ba04c` and
records that reuse explicitly; it must not train the 2,000 rows again merely to
chase the retired directory identity. Mixed and QAD remain fresh: run exactly
one fixed 8,000-example Mixed continuation with a fresh optimizer, then one
fresh QAD student. Every 16-example Mixed learning window contains 8 Long,
4 Identity, and 4 Noisy examples; seed 17,029 and the frozen schedule are
immutable. Mithril H200 runs use `h200_full_capacity_v1`: physical batch 16,
accumulation 1, and no gradient checkpointing while preserving the same
16-example optimizer windows and equal per-example weighting.

After the Mixed teacher is complete, QAD starts fresh on exactly 6,000 unique,
deterministically shuffled rows: all 2,000 original contexts, all 2,000
Identity children, and all 2,000 Noisy children. It may export only llama.cpp
b10158 QAD-Q4_0 and must run the bound 200-long plus 400-short regression
through one persistent server. No Development or historical QAD weight is a
fallback. This Production run is not by itself publication or product-release
authorization.

The only quality source is the 2,000-pair Word corpus bound in
`fresh_windows_lfm2_350m/data/dataset_stats.json`. The short-recovery builder
may read only that metadata and its exact `train.jsonl` and `validation.jsonl`
siblings after verifying their recorded sizes and SHA-256 values. It derives
short examples only from each row's high-quality target. It does not open
`test.jsonl` or use any product-E2E case as training data. The five cases that
exposed the first candidate's failures are now regression evidence, not an
independent final gate; create additional previously unused product cases only
after the recovery recipe is frozen.

The active derived input is create-once `short_aug_v3`, bound by manifest
SHA-256 `29a6e3ed62bd0123b0923a23784cbb93d59213ffb6039750c0f8e630fb4c6509`.
It contains 3,200/3,200 unique training pairs and 400/400 unique validation
pairs. `short_aug_v2` is retained only as rejected duplicate-preflight evidence
and must not be trained or evaluated.

PRAXIST training and Development evaluation run through the active task's
`evaluations/primary/run.py`. The evaluator records exact data, prompt, model,
parent adapter, configuration, seed, and output bindings. Development trains
only the 3,200 target-derived short children from the 1,600 training parents.
The original 200 validation rows and their 400 short children remain evaluation
only and report separate long- and short-input regression metrics. This
validation was used in the earlier H1 selection and is therefore recipe
regression evidence, not independent final evidence.

A Production operator reconstructs the 2,000 bound parents from the Word
source and derives exactly one Identity and one Noisy child per parent. It
inherits no Development metric, adapter, optimizer, prediction, or model
weight. The owner acceptance authorizes only this fixed Mixed recipe.

## Input boundary

Allowed inputs are the bound Word corpus, its active split metadata, the pinned
LFM base snapshot, the active prompt, and deterministic short examples derived
from those new targets. Existing LFM code, QAD code, runtime locks, and prior
measurements are reusable engineering evidence.

The historical `ml/scriber_polishing` tree, earlier corpora and split roots,
Scriber databases or transcripts, SAPI/Qwen/Soniox source attempts, Boldt
artifacts, and the five fresh product-E2E cases are outside the training-data
boundary. Resolve searches to a named active file or directory; a missing input
produces a bounded diagnostic rather than a broader filesystem scan.

## QAD and release boundary

Liquid QAD-Q4_0 is the sole product quantization. High-precision parameters may
exist only as transient optimizer, merge, or frozen-teacher state needed to
train the QAD student. The product path creates, evaluates, and publishes no
PTQ, Q8, BF16, or alternative quantization artifact.

The owner confirmed aggregate annual revenue of USD 0 including affiliates and
described the work as open source, so the PRAXIST free-license revenue gate is
passed. Any distribution retains the exact attribution
`Praxist by Sapient Intelligence`.

The selected QAD winner is public at immutable Hugging Face revision
`d64f8a14a09b2916000d969edd18bc411745e53a`. It passed the bound 200 Long plus
400 Short regression set exactly and is anonymously downloadable. The earlier
0-of-5 product candidate remains historical failure evidence only. Product
integration now uses the direct install and runtime-smoke path requested by the
owner; do not rebuild the abandoned signed-receipt or sealed-environment gate.
Scriber exposes only this local `qad_q4_0` variant. The former local Gemma
catalog entries and cached weights are removed; existing online API models are
unaffected.

## Completion evidence

An iteration is complete only when the PRAXIST run is terminal, its dashboard
renders the long- and short-input metrics, every scheduler child has exited,
the selected recipe has one fresh all-data SFT plus QAD-only export, and the
Windows product E2E records its acceptance result. Technical wiring or a low
training loss alone never installs, activates, or publishes a model.
