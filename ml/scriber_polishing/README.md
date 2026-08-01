# Scriber Polishing

This isolated Python 3.12 project builds, validates, trains, evaluates, and
publishes the local `google/gemma-3-270m-it` Scriber transcript-polishing
engine. It does not modify Scriber's production Python environment.

All training and reference data is synthetic and AI-generated. Acceptance is
performed by independent AI critics plus deterministic validators; there is no
human curation or human gold set.

The release pipeline is fail-closed. Generated corpora, checkpoints, model
weights, caches, and credentials are never committed.

## V2 hard-case supplement

`configs/v2_hardcase_data.json` freezes only aggregate Sol/Terra error flags
and counts. The generator never reads individual judge cases, predictions,
references, test records, or challenge records. It creates independently
seeded synthetic siblings. The existing V1 120k corpus and every V1 artifact
remain unchanged.

The exact V2 data calculation is:

- 720 parent documents × 2 canonical siblings = 1,440 canonical records.
- 640 train parents × 2 siblings × 8 pairs = 10,240 train pairs.
- 80 validation parents × 2 siblings × 4 pairs = 640 validation pairs.
- Total: 10,240 + 640 = 10,880 synthetic pairs.

Parent assignment happens before sibling and corruption generation. Therefore
no parent can cross between train and validation. The manifest additionally
requires the split total, category total, parent-group total, and the equations
above to agree; any mismatch fails verification.

The V2 corpus has a mandatory two-stage provenance gate. Stage one creates only
an immutable review candidate. It contains no critic names, decisions, or
training-ready manifest:

```powershell
$env:PYTHONPATH = "src"
python -m scriber_polishing.v2_hardcase_dataset --prepare-review
python -m scriber_polishing.v2_hardcase_dataset --check
```

The output is `artifacts/v2_hardcase_data_v1/` with `plan.json`, three
`review-*.jsonl` files, and `review-package.json`. Its content SHA covers every
canonical and every proposed train/validation pair, but no review metadata.
The verifier checks contract schemas, byte hashes, aggregate-only provenance,
NFC normalization, pair and ID deduplication, protected values, count
derivation, and parent leakage. It rejects `test.jsonl` and `challenge.jsonl`.

Stage two requires four separate verdict JSON files bound to both the candidate
SHA and `contracts/v2_hardcase_review_verdict_schema.json`: one Terra fact
review, one Terra style review, and two independent Sol adversarial reviews.
All four must cover the entire candidate, attest that no test or challenge data
was opened, use maximum reasoning, return `acceptable: true`, and contain no
critical flag. The generator never fabricates these verdicts. Finalize with:

```powershell
python -m scriber_polishing.v2_hardcase_dataset --finalize-review `
  --evidence path/to/fact.json `
  --evidence path/to/style.json `
  --evidence path/to/adversarial-a.json `
  --evidence path/to/adversarial-b.json
python -m scriber_polishing.v2_hardcase_dataset --check
```

Only a successful finalization writes `manifest.json`, `train.jsonl`, and
`validation.jsonl` with `training_ready: true`. Each pair then carries the four
real reviewer identities and the exact reviewed candidate SHA. Any missing,
duplicate, dissenting, critically flagged, or hash-mismatched verdict fails
closed.

The later V2 training mix replays V1 data deterministically and split-locally:
10,240 records only from `artifacts/final_data_v1/splits/train.jsonl` plus 640
only from `artifacts/final_data_v1/splits/validation.jsonl`, combined 1:1 with
the reviewed V2 supplement for 20,480 train and 1,280 validation records. Test
and challenge inputs are forbidden. The V1 manifest and both permitted split
hashes are pinned; replay examples use seeded SHA-256 bottom-k selection and
both sources are then stably interleaved without changing any row.

After the four-critic V2 finalization succeeds, build and independently check
the training package with:

```powershell
python -m scriber_polishing.v2_training_mixer
python -m scriber_polishing.v2_training_mixer --check
```

The output is `artifacts/v2_training_mix_v1/` with only `train.jsonl`,
`validation.jsonl`, and a closed, hash-bound `manifest.json`. The manifest
records each source binding, selected-ID hashes, stable mixed-order hashes,
critic-evidence identity, duplicate and parent-leakage gates, and the exact
20,480/1,280 count derivation. `configs/train_v2_hardcase_mixed.yaml` is the
matching one-epoch continuation configuration. The supplement-only
`configs/train_v2_hardcase.yaml` remains diagnostic and is not the mixed V2
training configuration.

### Attempt-13 training postflight

The completed V2 run is accepted only through its metadata-only postflight.
The verifier binds HF job `6a6e05bf6b79c09949c1e5fd`, the immutable packet
and V1 parent trees, 640/640 steps, CUDA BF16, learning rate `5e-6`, the final
run fingerprint, BF16 model tree, weight SHA-256, protection policy, and the
in-job fresh-reload result. It reads only bounded JSON metadata and the
authoritative bucket file listing; it never reads or downloads model bytes:

```powershell
python scripts/verify_hf_v2_training_postflight.py `
  --launch-receipt path/to/launch-receipt.json `
  --job-inspect path/to/job-inspect.json `
  --training-report path/to/training-report.json `
  --training-result path/to/v2-training-result.json `
  --runtime-verification path/to/runtime-verification.json `
  --packet-verification path/to/packet-verification.json `
  --parent-binding path/to/parent-binding.json `
  --final-inventory path/to/final-checkpoint-inventory.json `
  --reload-verification path/to/final-reload-verification.json `
  --bucket-inventory path/to/bucket-inventory.json `
  --report path/to/new-completion-model-binding.json
```

The output conforms to
`contracts/hf_v2_training_completion_model_binding_schema.json`. Any changed
job identity, step count, runtime, fingerprint, tree, weight, policy, reload
evidence, or required remote file fails before a completion report is written.

### Attempt-14 compact evaluation outcome

The completed attempt-13 model was compared with the released V1 model in a
short, non-publication-grade smoke test. The local Windows fallback reused the
sealed attempt-14 packet after the Hugging Face evaluator had completed all 300
predictions but exposed a duplicate cross-suite case identifier. The repair
namespaced identifiers only in the combined and Terra handoff; the three
100-case selections and all predictions remained byte-identical. The recovery
path also verifies a before/after SHA-256 tree and never reruns inference.

The verified automatic result covered 100 AI-gold, 100 critical-regression,
and 100 regular-test cases:

- V1 normalized exact match: `0.546667`; V2: `0.530000`.
- V1 deterministic critical errors: `400`; V2: `407`.
- V1 structured-output parse rate: `0.88`; V2: `0.85`.
- Automatic compact gate: `failed`.

One separate `gpt-5.6-terra` session then judged 100 blinded A/B pairs with an
exact 34/33/33 suite split and V2 assigned to A and B 50 times each. V2 won 21
pairs, V1 won 15, and 64 were ties. Terra marked V2 critical in 24 pairs and V1
critical in 21, so the conservative Terra gate also `failed`. This evidence is
labelled `compact_smoke_test` and `publication_grade=false`.

The ignored local evidence remains hash-bound for a future synthetic hard-case
round without committing test or challenge content:

- evaluation result: `sha256:af63f66037498f77562d87b570ba93d8ca3d7dcee36d603738cddeb6584079e9`
- 300-case Terra source: `sha256:1acbcd92bc49a6413aa2da50799dbe06dbcb63119a69768c26ef251f94d51c24`
- canonical Terra verdicts: `sha256:8713b1a16d991c07ee40ec63d95eb3afc11fe65fdddd542c5eb6c378c436cbe6`
- Terra session: `sha256:4df090665d6d9e5d8e6d4b9af2146fdfa16b51474b57381d878aab27b9f1c2e3`

V2 is therefore a rejected improvement candidate: it is not quantized,
published, or selected by Scriber. The public V1 Q8_0 and BF16 artifacts remain
the application default and fallback. Future data work may use only aggregate
failure signals or independently generated synthetic siblings; it must not
train on these sealed test, challenge, prediction, or judge records.

### Gated attempt-15 BF16/Q8_0 conversion

Attempt 15 is admitted only after the compact 300-case V2-versus-V1 automatic
gate and the separate blind Terra-100 gate have both passed. Build the release
plan first, then package the cloud conversion job. Attempt 14 failed both
admission gates, so attempt 15 was not packaged, claimed, launched, quantized,
or published. The commands below document the dormant fail-closed path for a
future candidate that passes both gates:

```powershell
python scripts/package_hf_v2_release_job.py `
  --plan path/to/v2-release-plan.json `
  --evaluation-binding path/to/v2-evaluation-binding.json `
  --output path/to/attempt15-packet
```

The packet contains the exact tracked source and accepted evidence, but no
model weights. The completed attempt-13 model and verified llama.cpp v19 bundle
are mounted read-only inside Hugging Face; only BF16 and Q8_0 are produced.
Packaging and verification are local-only. After the immutable packet has been
uploaded to its contract URI, the mutating launch remains a separate explicit
step:

```powershell
python scripts/launch_hf_v2_release_job.py `
  --packet path/to/attempt15-packet `
  --receipt path/to/attempt15-launch-receipt.json
```

The launcher reconciles API and CLI job inventories three times, refuses any
active or prior attempt-15 identity, requires an empty output prefix, acquires
one permanent compare-and-swap claim, and makes exactly one detached submit
call. An ambiguous response retains the claim and forbids an automatic retry.
It does not publish the resulting files. Public staging, commit binding, and
anonymous HEAD plus one-byte Range verification remain separate release gates;
V1 stays the application fallback until those gates pass.

## Deterministic automatic evaluation

Frozen references can produce two no-model baselines: `raw_transcript` emits
the source unchanged, while `deterministic_minimal` removes only unprotected
non-semantic fillers, normalizes whitespace, applies initial capitalization and
terminal punctuation, and wraps one safe SST paragraph. Literal SST-looking
tokens are rendered with full-width brackets so they remain text rather than
becoming executable formatting.

```powershell
python scripts/build_standard_predictions.py `
  --references path/to/references.jsonl `
  --candidate raw_transcript `
  --output path/to/raw.predictions.jsonl `
  --manifest path/to/raw.predictions.manifest.json
python scripts/evaluate_predictions.py `
  --references path/to/references.jsonl `
  --predictions path/to/final.predictions.jsonl `
  --candidate final `
  --config configs/evaluation.yaml `
  --output path/to/final.evaluation.json
python scripts/compare_paired_evaluations.py `
  --base-report path/to/base.evaluation.json `
  --fine-tuned-report path/to/final.evaluation.json `
  --output path/to/base-v-final.significance.json
```

The automatic report calculates exact normalized-text CER and WER, plus a
macro chrF++ F2 score over character 1–6-grams and word 1–2-grams. CER/WER use
an exact bounded Levenshtein implementation; an over-budget comparison is
reported as `unknown`, never approximated. Capitalization and sentence-boundary
scores require a source/target alignment that makes the intended change exact.
Self-correction, spoken-formatting, grammar, spelling, and orthography scores
are exact-recovery proxies only on records carrying the corresponding operation
tag. AST-derived heading, paragraph, list, inline-emphasis, unit, and legal
metrics are likewise emitted only when their reference feature exists.

Address-pronoun accuracy requires a preserved `address_mode` plus an applicable
reference pronoun. Semantic hard checks use only declared fact protected values,
protected entities, and conservative explicit answer/summary cues. They can
detect anchored omission/reordering and cue-marked additions, answers, or
summaries; they deliberately do not claim to detect unrestricted paraphrase,
implicit additions, or general semantic equivalence. Raw blank-line intent,
grammar/spelling quality without operation metadata, pronoun correctness without
address metadata, model eval loss/perplexity, and human/judge rubric quality are
intrinsically unavailable to this deterministic prediction-only layer and stay
`unknown` rather than being recorded as zero or passing.

The paired report binds both input-report hashes, reference/config hashes,
sorted case-ID hash, metric, seed, resample count, and nearest-rank confidence
interval. Its decision is significant only when the lower paired-bootstrap bound
strictly exceeds the declared minimum delta. The repository release policy
requires this measured `base_model` versus `final` evidence before `final` can
receive a release status.

## Remediation A/B adoption

`build_remediation_experiment.py` freezes one validation-only, matched
`improvement` experiment. The bundle carries its exact
`comparison-policy.yaml` alongside the two order plans, configs, cohort, and
parity-dev references, so the later decision needs no mutable source config.
Both arms must start from the same parent with fresh optimizer/scheduler state;
only the order mode and resulting epoch order may differ.

After both real runs and their raw batch evaluations complete, compare them
with the bundle-specific entrypoint:

```powershell
python scripts/compare_remediation.py `
  --bundle-manifest path/to/bundle/experiment-manifest.json `
  --references path/to/bundle/parity-dev.references.jsonl `
  --shuffled-training-report path/to/shuffled.training.json `
  --shuffled-predictions path/to/shuffled.predictions.jsonl `
  --shuffled-prediction-manifest path/to/shuffled.predictions.manifest.json `
  --shuffled-evaluation-report path/to/shuffled.evaluation.json `
  --staged-training-report path/to/staged.training.json `
  --staged-predictions path/to/staged.predictions.jsonl `
  --staged-prediction-manifest path/to/staged.predictions.manifest.json `
  --staged-evaluation-report path/to/staged.evaluation.json `
  --output path/to/remediation-comparison.json
```

Batch inference and evaluation must label the candidates `shuffled` and
`staged`. The comparator rejects Pilot reports, incomplete steps, changed
parent/policy/cohort/training contracts, and mismatched report, prediction, or
evaluation hashes. It independently recalculates deterministic metrics.
`ADOPT_STAGED` requires every absolute protected-span gate, no new critical
errors, non-inferior SST and task-quality metrics, eval loss at most 1.02×,
composite delta at least 0.005 under fixed 60/25/10/5 weights, and a strictly
positive lower 95% bound from the pre-bound 10,000-sample paired bootstrap.
Every measured failure yields `KEEP_SHUFFLED` plus `NO_ADOPTION`; unverifiable
inputs fail without fabricating a comparison report.

## Product lifecycle status

Externally visible release reports use only the lifecycle values from section
48 of the project goal: `AI_DATA_FACTORY_IN_PROGRESS`, `READY_FOR_TRAINING`,
`TRAINING_IN_PROGRESS`, `READY_FOR_AI_JUDGING`, `RELEASED`,
`PRIVATE_CANDIDATE`, `BLOCKED_BY_INCLUDED_CODEX_LIMIT`,
`BLOCKED_BY_EXTERNAL_PREREQUISITE`, or `FAILED`. Judge aggregation may retain
the internal state `pending_tiebreak`; release verification exposes that state
as `READY_FOR_AI_JUDGING`.

`verify_release.py` binds every supplied artifact by SHA-256. It requires a
schema-valid completed final-training report and a verified private Hub
upload/reload report before it can emit `PRIVATE_CANDIDATE`. The Hub report's
training-report hash, run fingerprint, and BF16 final-model tree hash must match
the separately supplied final-training report. Without verified private
publication, a trained candidate that has not completed release gates stays
`READY_FOR_AI_JUDGING`; without completed-final-training evidence it stays
`READY_FOR_TRAINING`. `RELEASED` additionally requires every recomputed
automatic, judge, paired-significance, and enabled-variant component to pass.

## Final checkpoint selection

`configs/checkpoint_selection.yaml` selects within one completed final run. It
does not name candidates: the selector discovers the tracked best-loss
checkpoint (when present) plus the configured recent tail from the immutable
completed training report's surviving checkpoint inventory. When best-model
tracking is disabled, the report must contain a consistent `null`/`null` best
checkpoint/metric pair and selection uses exactly the recent tail. Candidate
IDs must be dynamic `checkpoint-<step>` values—no extras or omissions. A
completed step may be newer than the last saved checkpoint when the terminal
step is not a save boundary.

Each candidate binds the run fingerprint, training checkpoint identity and
step, complete BF16 inventory, tree/config/model hashes, reference and
prediction manifests, generated deterministic automatic report, and that
checkpoint's `eval_loss`. No final judge evidence is used at this pre-holdout
stage. Safety gates rank before a generated composite: safety/content 60%, task
25%, structure 10%, and inverse normalized eval loss 5%. Ties use pass status,
composite, critical errors, generated subtotal, eval loss, then lower step.

```powershell
python scripts/select_final_checkpoint.py `
  --completed-training-report path/to/completed-final-training.json `
  --candidates path/to/candidate-evidence.json `
  --output path/to/checkpoint-selection.json `
  --handoff-output path/to/checkpoint-selection-handoff.json
python scripts/prepare_selected_checkpoint_publication.py `
  --handoff path/to/checkpoint-selection-handoff.json `
  --publication-evidence path/to/selected-checkpoint-publication-evidence.json `
  --output path/to/selected-checkpoint-publication.json
python scripts/verify_selected_checkpoint_publication.py --list-hooks
```

The selector emits an immutable selection handoff. The later publication stage
validates only the selected checkpoint, pinned base tokenizer, and fresh
reload/inventory evidence before its listed manifest, fresh-reload, and private
publication hooks. None of these commands loads a model, uses a GPU, or
contacts a hub.

## Fail-closed product inference

Single-text inference uses the product safety layer. The polished result is the
only value written to standard output. Content-free diagnostics are written as
JSON to standard error, or atomically to `--diagnostics-output`. This keeps
rendered user text and operational evidence separate.

```powershell
python -m scriber_polishing.inference `
  --model path/to/local-model `
  --text "Das Rohtranskript" `
  --output-format plain `
  --warmup `
  --diagnostics-output path/to/inference-diagnostics.json
```

Product inference is deterministic greedy decoding (`do_sample=False`,
`num_beams=1`) with the training prompt. Before generation it replaces amounts,
legal references, norms, URLs, email addresses, stable identifiers, and every
digit-bearing number with exact-once placeholders. Afterwards it restores the
placeholders, parses the closed SST language, validates the AST, validates
critical content, and only then invokes the allowlist renderers.

The fallback order is fixed:

1. Insert only unambiguous missing SST line boundaries. The repair is accepted
   only if parsing succeeds and every non-whitespace text atom is unchanged.
2. If structure remains ambiguous, remove only known SST markup and emit
   conservative plain text. Unknown tags are never stripped or emitted.
3. If placeholder, AST, or content validation fails, return the complete
   original transcript.

Diagnostics conform to
`contracts/product_inference_diagnostics_schema.json`. They contain a source
hash and length, not the transcript or generated text, and assign every repair
or failure an active-learning priority.

Texts above `--max-chunk-characters` (default 3000) are partitioned exactly at
paragraph or conservative sentence boundaries. A protected value is never
split. A sentence longer than the target remains one oversized chunk; the limit
is deliberately soft. Chunk ASTs are recombined and validated as one document.
If one chunk is unsafe, the complete original document is returned. If any
chunk requires plain-text cleanup, the whole result is plain text.

These checks are intentionally conservative and deterministic. They detect
changed critical literals, numbers, amounts, legal references, norms, polarity,
modal verbs, gross content loss/addition, reordered critical spans, invented
headings, and invalid block order. They do not prove unrestricted semantic
equivalence, subtle paraphrase fidelity, entity resolution, or discourse
coherence. Such cases remain the responsibility of held-out evaluation and
model judges; runtime uncertainty fails closed where a deterministic check is
available.

Batch mode (`--references`) intentionally remains raw evaluation inference:
it records the model SST completion and parse/restoration evidence without
product repair, cleanup, content fallback, or chunk recombination. This
separation preserves fair candidate comparisons.

For the private Hugging Face quantization job, a completed cloud toolchain can
be bound through `prepare_hf_quantization_job.py
--llama-cpp-materialization-result <result.json>` while omitting the local
bundle and lock options. Only the small canonical cloud-produced result is
read locally; the just-produced toolchain payload is not downloaded. The
quantization job mounts that payload read-only and verifies its complete lock,
runtime closure, and tool hashes before conversion or inference.

The pinned CUDA toolchain is materialized on `cpu-basic` by default. This stage
only extracts and inventories the reviewed OCI layers and runs loader/version
probes; it does not execute model inference or require a GPU device. The exact
PyTorch image digest still supplies the CUDA, cuBLAS, and NCCL runtime closure.
Only the exact unresolved `libcuda.so.1` SONAME may remain as a symbolic,
lock-bound host-driver requirement on this CPU stage; every other unresolved
library fails closed. Before the later GPU job uses llama.cpp, it must load the
real driver, initialize CUDA, enumerate at least one device, and re-probe a
fully resolved `ldd` closure. GPU capacity is reserved for that quantization
and evaluation job.

## Private Hugging Face publication

Publication uses two fixed, separate private repositories for the model and
synthetic dataset. First prepare and hash-bind both upload folders locally:

```powershell
python scripts/prepare_private_hub_bundle.py `
  --model-artifact path/to/model-bundle `
  --dataset-artifact path/to/dataset-bundle `
  --completed-training-report path/to/completed-final-training.json `
  --selected-checkpoint-publication path/to/selected-checkpoint-publication.json `
  --report artifacts/hub_preparation.json
```

This command performs no network operation. It validates the completed BF16
final-training report and selected-checkpoint handoff, verifies that the
published config and model bytes are the selected checkpoint, writes immutable
bindings into the model bundle, scans every file for secrets and private local
paths, and inventories every required card, legal notice, report, runtime,
split, and checksum file.

The model bundle must already contain the fixed non-holdout regression suite.
Select its inputs only from existing synthetic train/validation and AI-Gold
train/validation records; frozen test and challenge data are rejected:

```powershell
python scripts/build_hub_regression_sources.py `
  --synthetic path/to/train.jsonl `
  --synthetic path/to/validation.jsonl `
  --ai-gold path/to/ai_gold_train.sealed.jsonl `
  --ai-gold path/to/ai_gold_validation.sealed.jsonl `
  --sources-output artifacts/hub-regression/sources.jsonl `
  --references-output artifacts/hub-regression/references.jsonl `
  --report-output artifacts/hub-regression/source-selection-report.json
```

Run the selected model on the emitted references, then materialize the exact
source/prediction join:

```powershell
python scripts/build_hub_regression_suite.py `
  --sources path/to/hub-regression-sources.jsonl `
  --predictions path/to/hub-regression-predictions.jsonl `
  --output path/to/model-bundle/examples/regression_sample.jsonl
```

After the automatic, judge, benchmark, and variant-matrix reports are complete,
assemble both upload trees without network access. The assembler hard-links
unchanged large files when possible, refuses non-empty output roots, includes
the Gemma derivative notices and Apache dataset license, and never reads from
Hugging Face:

```powershell
python scripts/assemble_private_hub_artifacts.py `
  --model-source path/to/final-model `
  --dataset-source path/to/final-data `
  --ai-gold-source path/to/ai-gold `
  --evaluation-root path/to/final-evaluation `
  --completed-training-report path/to/training-report.json `
  --variant-matrix-report path/to/variant-release-matrix.json `
  --benchmark-report path/to/selected-variant-benchmark.json `
  --terra-judge-report path/to/terra-judge-report.json `
  --sol-judge-report path/to/sol-judge-report.json `
  --regression-suite path/to/regression_sample.jsonl `
  --selected-variant Q8_0 `
  --quantized-artifact Q8_0=path/to/q8-artifact `
  --quantized-artifact Q4_K_M=path/to/q4-artifact `
  --output-root path/to/new-empty-upload-trees
```

For a completed early-stopped final run, bind the exact training report and
local model tree to the already completed full-model evaluation and fresh BF16
laptop benchmark before preparing the Hub bundle:

```powershell
python scripts/prepare_completed_final_model_publication.py `
  --completed-training-report path/to/training-report.json `
  --model path/to/final-model `
  --final-evaluation-result path/to/final-evaluation-result.json `
  --model-binding path/to/model-binding.json `
  --benchmark-report path/to/bf16.json `
  --evidence-output artifacts/completed-model-publication-evidence.json `
  --output artifacts/selected-checkpoint-publication.json
```

The command is offline and does not load the model. It verifies every file in
the training inventory, the final same-run checkpoint identity, the evaluation
tree binding, and the independently executed CUDA benchmark before emitting a
publication handoff. This avoids running a second model reload solely for
publication evidence.

The mutating publication command requires `HF_TOKEN` with proven write role. It
checks namespace authorization and both private repositories before the first
upload, pins the exact commit returned by each upload, verifies Hub file sizes
and LFS SHA-256 values, and matches ordinary Git files to their exact remote
blob IDs. By default it does not download bytes that were just uploaded:

```powershell
python scripts/publish_private_hub.py `
  --model-artifact path/to/model-bundle `
  --dataset-artifact path/to/dataset-bundle `
  --completed-training-report path/to/completed-final-training.json `
  --selected-checkpoint-publication path/to/selected-checkpoint-publication.json `
  --verification-root path/to/new-empty-verification-root `
  --report artifacts/hub_verification.json `
  --report-markdown artifacts/hub_verification.md
```

The unchanged local upload sources are SHA-256-verified and model/tokenizer plus
all configured dataset and AI-Gold splits are reloaded offline in fresh isolated
Python processes with credentials removed. The model
reload reruns at least 100 exact-output regressions (including AI-Gold smoke,
protected spans, SST round-trips, and all five renderers) and binds the fresh
reload evidence for every eligible variant. Variants that fail before full
qualification remain listed as tested, ineligible variants with a stage,
reason code, and immutable evidence hash; they produce a private-candidate
matrix rather than being silently omitted. Technically valid variants that
complete reload, evaluation, and benchmark but miss an automatic quality gate
remain eligible for private inspection; a separate qualification-failure entry
is bound to the exact evaluation or quality-delta report. This prevents a
quality failure from being mislabeled as a broken artifact while ensuring that
the matrix can never claim release readiness. Mixed cloud/local evidence is
assembled deterministically with
`scripts/build_private_candidate_release_matrix.py`; its source manifest binds
all three frozen suites, artifact manifests, benchmark reports, prediction
packages, and failure evidence before publication. Any missing legal
file, changed hash, wrong revision, non-private repository, read-only token,
inherited cache, local fallback, or failed reload prevents creation of the
immutable JSON and Markdown verification reports.

The legacy `--verification-mode fresh_exact_revision_download` remains
available for investigations that explicitly require a post-upload roundtrip;
normal publication uses `local_source_remote_identity`.
