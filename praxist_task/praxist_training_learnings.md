# PRAXIST training learnings

Last updated: 2026-09-02

## Current decision

- The active fresh run lives in
  `praxist_task/fresh_windows_lfm2_350m`; its post-Complete operator is
  `praxist_task/fresh_windows_lfm2_350m_operator`. The earlier paired task is
  historical evidence only.
- The active lineage uses neither historical Scriber polishing pipeline nor
  any earlier corpus, adapter, checkpoint, optimizer state, prediction, cache,
  or score as training state. The only data source is the explicitly supplied
  2,000-pair DOCX.
- Training and evaluation run natively on Windows. WSL is used only because
  PRAXIST 0.5.0 refuses native Windows research runs.
- The canonical LFM-only PRAXIST run
  `run_2026-09-01_09-51-15-034106_fresh_windows_lfm2_350m` is final and
  succeeded: exit code 0, one committed generation, seven scheduler jobs
  completed, zero failed, and the H1 Complete score is `0.9715275`. Boldt is
  permanently retired; no further Boldt training, evaluation, recovery,
  comparison, final test, merge, quantization, or export is permitted.
- Product quantization is QAD-only: if a later release is authorized, it may
  publish exactly one genuine llama.cpp `QAD-Q4_0` artifact. Do not generate
  BF16/Q8_0/Q5_K_M/Q4_K_M/PTQ-Q4_0 or NVFP4 alternatives in parallel. BF16 is
  only the frozen teacher and a transient merge/training working state, never a
  second product candidate.
- The selected QAD winner is public at immutable Hugging Face revision
  `d64f8a14a09b2916000d969edd18bc411745e53a`, installs anonymously, and passed
  the bound 200 Long plus 400 Short regression cases exactly. The earlier 0/5
  candidate remains historical failure evidence only. On 2026-09-01 the owner
  explicitly confirmed aggregate annual revenue of USD 0 including affiliates
  and that the work is open source, so the PRAXIST free-license revenue gate is
  passed. Distribution retains the exact attribution
  `Praxist by Sapient Intelligence`.
- Reserve the final 200 rows as complete canonical topic families, including
  obvious title aliases. Split the remaining 1,800 rows into 1,600 training
  and 200 validation rows with seeded topic coverage and feature balancing.
  Do not split by running example number, and never use final-test performance
  during PRAXIST research.
- The chronological sections below intentionally retain superseded Boldt and
  conventional-matrix findings as failure/decision history. They are not
  current execution instructions; this Current decision and the final LFM/QAD
  sections override them operationally.

## Direct LFM promotion decision

- The accepted LFM2.5 W4 adapter is the score winner from the successful
  terminal PRAXIST Complete run: 1,600 training rows, 200 randomized
  validation rows, score 0.9325344805742523, critical-token F1
  0.9554813361097854, and exact critical-value preservation 132/200.
- The later paired reproduction failed in the Windows console boundary before
  its first optimizer step. Its attempt ledger nevertheless contains the
  immutable run-selection receipt that was created before the failed child
  started and binds the terminal PRAXIST run, variant, raw summary, adapters,
  base snapshots, prompt, evaluator, scheduler job, and task manifest.
- The user judged that terminal evidence sufficient and explicitly requested
  LFM-only continuation. Finalization therefore uses a distinct direct-LFM
  selection mode; it must never label the failed attempt a successful
  reproduction and may use no output from that attempt.
- The sealed 200-row final split remains one-shot. Freezing and validation may
  read only the raw Complete summary and provenance ledgers; only the canonical
  final evaluator may open `data/test.jsonl`, exactly once and only for LFM.

## Fresh LFM-only restart

- Before the sealed final split was opened, the user replaced the promotion
  objective with a stricter one: start LFM2.5 again from its untouched Base
  weights, use only the supplied high-quality 2,000-letter DOCX, and run no
  further Boldt work. The frozen direct-selection manifest therefore remains
  unused historical evidence; no final-test attempt or receipt was created.
- Reuse is encouraged for LFM-specific engineering and knowledge: the native
  Word-list extractor and split algorithm, the Windows GPU
  evaluator, the WSL PRAXIST control plane, the central single-GPU scheduler,
  the critical-token metrics, the dashboards, and W4/rank 8 as the first
  hypothesis. Do not reuse an adapter, checkpoint, optimizer state, prediction,
  cache, or score as new training state.
- The supplied DOCX currently has SHA-256
  `cea3fc836a59a108164058530994de4f6f08342bf37dc39524ebdbaec1e3240c`
  and 1,551,320 bytes. Earlier metadata bound its exact path but not its byte
  hash, and the earlier PRAXIST task manifest omitted `data/`; timestamps and
  path identity are strong but not cryptographic ancestry for those JSONLs.
  Therefore the LFM-only task must pin this DOCX hash and rerun the proven
  extractor into a new isolated data root instead of copying any old JSONL.
- A fresh LFM-only run must load
  `LiquidAI/LFM2.5-350M-Base` directly, train one LFM arm only, and write to a
  new task/run/result root. Its iterative validation may reuse the public
  seeded split algorithm because it operates on the newly supplied DOCX, not
  on the rejected historical corpora. The newly extracted sealed test stays
  closed until the new LFM-only winner is selected.
- Finalizer review found that immutable manifest metadata and exact field sets
  also need binding. The direct manifest schema was tightened to v2, all
  immutable fields are now hashed, nested evaluation-contract keys are exact,
  and tests guard arbitrary path-shaped additions plus each one-shot boundary
  independently.

## Windows and WSL findings

- Native Windows `praxist --version`, `doctor`, `resolve`, and explicit
  `monitor --run-dir` are useful, but `start` and `resume` are unsupported.
- Native `praxist start --help` can fail under CP1252 on the Unicode arrow in
  its help text. Set `$env:PYTHONUTF8 = '1'` before native CLI calls.
- WSL Praxist is `/opt/praxist/venv/bin/praxist`; it is intentionally not on
  `PATH`, so use the absolute path.
- WSL can reuse the current Windows ChatGPT login without another login:
  `export CODEX_HOME=/mnt/c/Users/Alexander.Immler/.codex`.
- With that value, WSL `praxist doctor --target codex --codex-native --json`
  reports `ok: true`.
- The WSL and Windows registries are separate. Monitor WSL runs from WSL, or
  give the Windows monitor the exact shared C: run directory.
- The RTX 4070 Laptop GPU is visible in both environments with 8,188 MiB VRAM.
- PRAXIST 0.5.0 scans and hashes task files before `resolve` and does not skip
  `.venv-win`. Set `PYTHONPATH` to the task's `praxist_shim` so the process
  excludes that native Torch environment. This changed resolve from a DrvFS
  stall to a successful manifest resolution.

## DOCX findings

- 2,000 tables, all exactly 4 rows by 1 column.
- Every table uses the labels `VORHER | FLAT TRANSCRIPT` and
  `NACHHER | POST-PROCESSED TRANSKRIPT`.
- Source cells contain one flat paragraph.
- Target cells contain 7-12 Word paragraphs and one native bullet or decimal
  list. The extractor must preserve list markers; `cell.text` alone loses them.
- The Word file has 97 topics, 16 formal error profiles, and 3/4/5 list-item
  counts. Singular/plural tag aliases are canonicalized for split balancing.
- The final split has 1,600/200/200 unique rows. Training and validation both
  cover the same 87 public topics, all 16 profiles, and all 11 canonical tags.
  Validation profile total-variation from the full corpus is 0.0265, list kind
  is 99/101, and every 250-number band contributes 22-30 rows.
- The final test holds out 10 topics in disjoint canonical families, has zero
  normalized source/target fingerprint overlap with public data, includes all
  16 profiles and 11 tags, and has exactly 25 rows from every number band. Its
  profile mix is intentionally an unseen-family challenge rather than an
  in-domain population estimate.

## Commands

Resolve from PowerShell through WSL:

```powershell
wsl.exe -d Ubuntu-24.04 -- bash -lc '
  export CODEX_HOME=/mnt/c/Users/Alexander.Immler/.codex
  export PYTHONPATH=/mnt/c/Users/Alexander.Immler/Documents/Github/Scriber/praxist_task/fresh_windows_350m/praxist_shim
  cd /mnt/c/Users/Alexander.Immler/Documents/Github/Scriber/praxist_task/fresh_windows_350m
  /opt/praxist/venv/bin/praxist doctor --target codex --codex-native --json
  /opt/praxist/venv/bin/praxist resolve . --codex-native
'
```

Start autonomously:

```powershell
wsl.exe -d Ubuntu-24.04 -- bash -lc '
  export CODEX_HOME=/mnt/c/Users/Alexander.Immler/.codex
  export PYTHONPATH=/mnt/c/Users/Alexander.Immler/Documents/Github/Scriber/praxist_task/fresh_windows_350m/praxist_shim
  cd /mnt/c/Users/Alexander.Immler/Documents/Github/Scriber/praxist_task/fresh_windows_350m
  /opt/praxist/venv/bin/praxist start --task-path . --codex-native \
    --model gpt-5.6-luna --cohort 2 --generations 3 --daemonize \
    --startup-timeout 60 --json
'
```

Live monitor:

```powershell
wsl.exe -d Ubuntu-24.04 -- bash -lc '
  export CODEX_HOME=/mnt/c/Users/Alexander.Immler/.codex
  /opt/praxist/venv/bin/praxist monitor \
    --task-path /mnt/c/Users/Alexander.Immler/Documents/Github/Scriber/praxist_task/fresh_windows_350m \
    --latest --follow
'
```

## Error log and prevention

1. **Native Windows scheduler rejection**
   - Cause: Praxist 0.5.0 supports the research runtime only on POSIX hosts.
   - Fix: controller in WSL, trainer in Windows.

2. **WSL provider authentication initially missing**
   - Cause: the WSL root account had no separate Codex login.
   - Fix: point `CODEX_HOME` at the existing Windows Codex directory.

3. **Native help text crashed with `UnicodeEncodeError`**
   - Cause: PowerShell process output used CP1252.
   - Fix: set `PYTHONUTF8=1`.

4. **Word list markers disappear with naive extraction**
   - Cause: `python-docx` `cell.text` returns paragraph text but not numbering.
   - Fix: inspect `w:numPr`, resolve the numbering definition, and materialize
     bullet or decimal prefixes while building the target string.

5. **PRAXIST resolve appeared to hang**
   - Cause: its manifest builder read and hashed the full `.venv-win`, including
     the large Torch installation, through WSL DrvFS. `.gitignore` is ignored.
   - Fix: the process-local `praxist_shim/sitecustomize.py` adds `.venv-win` to
     PRAXIST's manifest skip set. The task schema itself was valid.

6. **First grouped split covered all labels but distorted their frequency**
   - Cause: keeping whole topics out while also forcing every rare profile into
     a 200-row holdout left only 60 dominant-profile rows instead of about 105.
   - Intermediate fix: seeded 80/10/10 allocation within every topic restored
     representative frequencies, but an audit found strong template overlap.
   - Final fix: keep the representative split only for public validation and
     reserve the 200-row final test by complete canonical topic family. Exact
     normalized template overlap across that boundary is a hard failure.
   - The initial Canary is retained only as a technical smoke and is not
     selection evidence.

7. **LFM2 adapter save includes resized embeddings**
   - Cause: tokenizer/model vocabulary-size alignment resized the embedding
     layer, so PEFT conservatively saved it with the LoRA adapter.
   - Effect: valid training and inference, but a larger adapter artifact.
   - Follow-up: keep this behavior for correctness during research, then fold
     and export the final winner once.

8. **Ordered source examples can invalidate a naive numeric split**
   - Risk: the DOCX's error cases are partially ordered, so slicing by example
     number would give train and validation different error distributions.
   - Prevention: selection is seeded and feature-balanced, then every output
     split is shuffled again with seed `3502026` before JSONL serialization.
   - Direct audit: position/index correlation is -0.0305/-0.0252/+0.0346 for
     train/validation/test; ascending-neighbor rates are 50.91%/47.24%/48.74%,
     with no direct `+1` neighbor in any split. The 250-example bands contribute
     198-203 rows to train, 22-30 to validation, and exactly 25 each to test.

9. **Independent peers can propose an identical candidate**
   - Symptom: both generation-0 peers independently emitted the same seed and
     LoRA/optimizer settings, which would duplicate two-model GPU work.
   - Fix: the evaluator now reuses a protocol-passing result only when its
   effective training signature and evaluation stage are identical inside
   the same PRAXIST run. It never reuses results across runs.

10. **Fresh task was still routed through a retired Scriber terminal commit**
    - Symptom: the native trainer completed and wrote a valid summary, but the
      central scheduler changed the successful exit into infrastructure code
      75 because no old root commit supervisor acknowledged it.
    - Cause: the installed PRAXIST build contains an optional older
      Scriber-specific commit path that is unrelated to this fresh task.
    - Fix: `praxist_shim/sitecustomize.py` sets
      `SCRIBER_PRAXIST_SIMPLE_DIRECT=1` for every fresh-task controller. This
      preserves normal PRAXIST scheduling and direct result collection without
      bringing the retired pipeline back.

11. **The original 832-token limit was five tokens too small for LFM2.5**
    - Failure: development row `word-0603` required 837 tokens, after Boldt had
      already completed its arm.
    - Full-corpus measurement: Boldt maxima are 656/639/589 and LFM2.5 maxima
      are 837/823/747 for train/validation/test. No row exceeds 896.
    - Fix: use a measured fixed limit of 896 and cache each completed arm after
   validating model, config, stage sizes, seed metadata, metrics, and adapter
   files. A failed pair summary is no longer accepted as a success cache.

12. **Character similarity hid unacceptable value changes**
   - Symptom: the first valid development comparison looked very strong under
     protocol v1, although review samples changed amounts, areas, and a data
     rate. Examples were `193.495 € -> 103.945 €`, `491 m² -> 424 m²`, and
     `217 Mbit/s -> 228 MB/s`.
   - Cause: the v1 composite assigned 70% to character similarity and only 20%
     to a permissive critical-token F1. A nearly identical sentence could
     therefore retain a high score after changing its decisive value.
   - Fix: protocol v2 raises value preservation to the dominant signal and
     reports both multiset F1 and the stricter per-example exact preservation
     rate. Units are measured separately. The extractor covers German-formatted
     amounts, dates, times, addresses, phone numbers, legal citations, and all
     physical units present in the fresh corpus. Decimal list indices are
     deliberately excluded so that list numbering cannot inflate value scores.
   - Regression guard: seven focused tests now cover exact values, corrupted
     amounts/areas, `Mbit/s` versus `MB/s`, changed legal citations, five-digit
     postcodes, phone numbers, list indices, and false matches of short unit
     symbols inside words.
   - Consequence: v1 and v2 scores are not comparable. No v1 result may be
   reused by the next PRAXIST run; protocol identity and all v2 arm metrics
   are checked before same-run reuse.

13. **Cross-runtime metadata must be finalized on the WSL side**
   - Cause: PRAXIST peer/generation variables exist in the WSL evaluator, but
     Windows interop did not reliably expose them to `train_pair.py`.
   - Fix: after a successful native trainer exit, the WSL wrapper writes the
     current peer and integer generation identity into both the top-level
     summary and `extra`. Same-run reuse applies the same projection.
   - Guard: summary schema, protocol name, protocol version, stage, complete
     adapter inventory, nested arm metrics, top-level metrics, and exact pair
     means must all agree before an existing result can be reused.

14. **The v2 fidelity signal is populated for the whole fresh corpus**
   - Audit: every one of the 2,000 targets contains critical content. Per row,
     train/validation/test contain at least 5/5/5 critical tokens and 3/3/3
     unit tokens; the mean critical-token counts are 8.709/8.590/8.300.
   - Consequence: the per-example exact-preservation rate cannot be inflated by
     empty examples. Every validation row tests real values and units.

15. **The old development score overstated fidelity in every inspected row**
   - Offline v2 rescoring of the ten stored public review rows gave 0/10 exact
     critical-value preservation for both models. On those rows the v2 scores
     are 0.6284 for Boldt and 0.7015 for LFM2.5, versus the much higher v1
     aggregate impression.
   - LFM2.5 still leads qualitatively, but the failures are systematic rather
     than isolated: addresses, amounts, measurements, dates, times, and legal
     references can each be changed.
   - Consequence: the new PRAXIST run starts from fresh protocol-v2 evaluation;
     the stored v1 adapters are not promoted or reused.

16. **`praxist resolve` does not accept `--json` in PRAXIST 0.5.0**
   - Symptom: the first resolve invocation exited immediately with an argument
     parser error.
   - Fix: run `praxist resolve . --codex-native` without `--json`; its normal
     output is already structured JSON. `praxist start` still accepts `--json`.

17. **Same-run duplicate reuse now saves real GPU time**
   - Generation-0 peers independently proposed the same rank-8 Canary.
   - The first paired run completed in 570 seconds. The second scheduler job
     then verified protocol v2, stage, seed, effective arm configuration,
     complete adapters, nested/top-level metric agreement, and pair means, and
     reused it in about 2 seconds instead of training both models again.
   - The copied summary was rewritten with the destination variant, peer,
     generation, output path, and reuse provenance; its dashboard was rerendered.

18. **WSL cannot attribute the native Windows CUDA process by PID**
   - Symptom: the PRAXIST scheduler may report `no_gpu_process_observed` while
     Windows `nvidia-smi` shows the native trainer using the RTX 4070.
   - Cause: training is a Windows process launched through WSL interop, outside
     the scheduler's Linux PID view.
   - Safety: the central scheduler still owns a single logical GPU reservation
     and enforces concurrency 1. Windows `nvidia-smi`, the streamed trainer log,
     and nonzero VRAM/utilization provide the runtime evidence.

19. **The main remaining defect is value copying, not document structure**
   - A field-level audit of ten public protocol-v2 review rows confirms that
     both models often generate plausible values from the training distribution
     instead of copying the value in the current transcript.
   - Boldt preserved 38 of 77 expected critical tokens, with 39 missing and 34
     extra tokens; its review macro F1 was 0.5069. LFM2.5 preserved 47 of 77,
     with 30 missing and 30 extra tokens; its review macro F1 was 0.5889.
   - Neither model preserved a single one of the eight reviewed money values.
     Representative LFM2.5 corruptions include `193.495 € -> 103.945 €`,
     `491 m² -> 424 m²`, `217 Mbit/s -> 228 MB/s`, and
     `§ 129 Abs. 1 -> § 199 Abs. 1`. These are content changes, not harmless
     formatting differences.
   - LFM2.5 has nearly solved layout: list order was exact in 10/10 reviewed
     rows, paragraph count in 9/10, legal passages in 5/8, all 13 times, and
     15/17 dates were preserved. It introduced no new filler words. Boldt
     reached only 3/10 exact list orders, 5/10 paragraph counts, 0/8 legal
     passages, 12/13 times, and 13/17 dates, and introduced five filler words.
   - Interpretation: LFM2.5 is the stronger current base, while Boldt is also
     undertrained. Both development losses were still falling at their final
     step, so the next experiment should increase useful optimizer exposure
     without sacrificing LFM2.5's already learned structure.
   - Highest-priority hypothesis: `longer-gentler`, using two epochs and a
     learning rate of 0.0001 for both arms while keeping the other development
     settings fixed. This approximately doubles optimizer steps and reduces
     destructive updates.
   - Isolating comparison: `update-density`, changing only gradient
     accumulation from 16 to 8. This also approximately doubles optimizer
     steps, but without a second data pass, and distinguishes optimizer-step
     density from additional example exposure.
   - Capacity comparison: keep the effective LoRA scale constant while raising
     Boldt to rank/alpha 32/32 and LFM2.5 to 16/32.
   - Decision rule: critical-value exact preservation must improve materially.
     If the longer/gentler run does not do so, further ordinary hyperparameter
     tuning is unlikely to solve the copying defect; the next intervention must
     explicitly weight value-copy fidelity in the loss or training examples.

20. **Canary-to-development changes do not identify the LoRA-rank effect**
   - One PRAXIST finding described the rank-16 development candidate as an
     improvement over the rank-8 Canary. That comparison is useful for deciding
     to continue, but it is not a controlled ablation: the two stages use
     different training and validation sizes as well as different rank.
   - Consequence: do not attribute the development gain causally to rank 16.
     A same-stage rank-8 control would be required to measure that effect.
     Until then, rank 16 is only the configuration of the current candidate,
     not a proven reason for its score.

21. **Fresh GGUF export has two model-specific requirements**
   - The installed product runtime is exactly llama.cpp build 10158, commit
     `f87067841`, at Scriber's `backend/tools/local-polishing/llama-server.exe`.
     Local base-model smoke evidence confirms that this exact server can load
     and generate from BF16 and Q8_0 GGUFs for both Boldt and LFM2.5. The final
     merged adapters still require their own end-to-end verification.
   - LFM2.5's public base has 65,536 embedding rows, while the tokenizer and
     fresh adapter use 64,400. Loading the adapter directly into the untouched
     base fails with an embedding and language-head size mismatch.
   - Verified merge order for LFM2.5: load its tokenizer, resize the base to
     `len(tokenizer) == 64400`, load the adapter, then run
     `merge_and_unload(safe_merge=True)`. Preserve the resulting
     `config.vocab_size = 64400`; never overwrite that config with the original
     65,536-token base config after merging.
   - PEFT confirms this path during full training by setting
     `save_embedding_layers=True` after the vocabulary resize. The LFM adapter
     therefore intentionally contains the resized embedding and language-head
     weights; treating it as an ordinary shape-neutral LoRA adapter is invalid.
   - Boldt merges directly at its 32,000-token vocabulary, but llama.cpp b10158
     does not recognize the model's ByteLevel pre-tokenizer fingerprint. The
     fresh exporter therefore needs a narrow independent tokenizer override.
     The existing implementation is coupled to a retired training pipeline and
     is technical evidence only; it must not be reused as the export pipeline.
   - The product lock intentionally bundles only `llama-server.exe` and its
     runtime libraries. A clean b10158 converter checkout and its Python
     dependencies are not currently present. Materialize the exact commit only
     after the winner is frozen, then create both BF16 and Q8_0 directly with
     `convert_hf_to_gguf.py`; a separate quantizer is not required for Q8_0.
   - Vulkan was unavailable in the existing base-model smoke because the local
     driver reported `ErrorExtensionNotPresent`; CPU fallback succeeded. Final
     acceptance must therefore test both the attempted GPU path and the actual
     fallback path, followed by Scriber's real protect/completion/safety flow.

22. **Use a short Windows path for the exact llama.cpp source checkout**
   - Failure: cloning b10158 below the long AppData build-cache path reached
     Windows' path limit in deeply nested llama.cpp UI files, leaving an
     incomplete checkout even though Git had resolved the correct commit.
   - Fix: clone with `core.longpaths=true` to the short independent path
     `C:\sm\llama-b10158`.
   - Verification: the clean checkout resolves to the exact runtime-lock commit
     `f87067841bac583bc089a225382248d857791ca8` and has no working-tree changes.
     Use this checkout for the fresh converter; do not use the incomplete long-
     path cache and do not import the retired Scriber export pipeline.
   - A separate converter environment now exists at
     `C:\sm\gguf-b10158-venv`. It uses the exact b10158 requirements, including
     CPU Torch 2.11.0, Transformers 4.57.6, SentencePiece 0.2.2, and the
     checkout's GGUF package. `convert_hf_to_gguf.py --help` starts successfully
     and exposes both `bf16` and `q8_0`. This keeps all converter dependencies
     out of the still-active Windows training environment.

23. **A fresh standalone exporter can cover both model-specific merge paths**
   - `fresh_windows_350m_operator/export_fresh_winner.py` is independent of all
     retired Scriber training and export modules and never opens corpus files.
   - It requires explicit local paths for the frozen adapter, base snapshot,
     merge Python, converter Python, exact b10158 checkout, and new output root.
     Hub access and model credentials are removed from child environments.
   - It validates the LFM 65,536-to-64,400 resize before adapter loading, tied
     embeddings after safe merge, Boldt's 32,000-token layout, BF16 floating
     weights, absence of remaining LoRA parameters, exact clean llama.cpp
     commit, GGUF magic/size, and artifact hashes. Outputs are BF16 and Q8_0.
   - The Boldt converter override is implemented locally and narrowly from the
     exact tokenizer state and fingerprint; it imports no old Scriber code.
   - Independent verification: Python compilation passes and all 10 focused
     unit tests pass. Actual GGUF generation deliberately waits until PRAXIST
     freezes the winner; unit success is not runtime or quality evidence.

24. **Full-corpus evidence narrows LFM2.5's residual error to literal numbers**
   - In the ten public complete-stage review rows, LFM2.5 preserved 71/77
     critical tokens; six were missing and replaced by six other values. Five
     of ten outputs were fully exact. Boldt preserved 59/77 and produced no
     fully exact review output.
   - LFM2.5 now preserves 5/8 reviewed money values, all 7 legal citations, all
     17 dates, all 13 times, all ten list orders, and all paragraph structures.
     It introduces no filler word. Its six reviewed residual corruptions are
     `491 m² -> 401 m²`, `217 Mbit/s -> 211 Mbit/s`,
     `53.269 € -> 53.690 €`, `256 kg -> 246 kg`,
     `52.254 € -> 52.250 €`, and `20.630 € -> 20.631 €`.
   - Boldt still changes all eight reviewed money values, one legal citation,
     and two list contents, and reintroduces `ähm`/`quasi` in one row.
   - This supersedes the earlier development-stage experiment priority. At
     full-corpus scale LFM2.5's last loss is already about 0.009 and all
     non-literal behavior is nearly solved. The cleanest next controlled
     experiment is therefore to keep Boldt unchanged and change only LFM2.5
     from rank/alpha 8/16 to 16/32, preserving `alpha / rank == 2` while adding
     adapter capacity for literal copying.
   - Run that hypothesis at development stage first. If strict critical-value
     exactness does not improve, the next distinct hypothesis is the previously
     documented two-epoch, learning-rate-0.0001 `longer-gentler` variant.

25. **Generation 1 independently converged on the same LFM2.5 capacity test**
   - Both PRAXIST peers proposed the same effective numerical configuration:
     keep Boldt at rank/alpha 16/16 and change LFM2.5 from rank/alpha 8/16 to
     16/16. One canary is running under the single-GPU concurrency limit; the
     other is queued. Their `design_dimensions` labels differ, but their
     training hyperparameters and seed are identical.
   - This agrees with the evidence that LFM2.5 adapter capacity is the next
     useful intervention, but it is not the exact 16/32 experiment proposed in
     item 24. Rank rises while alpha stays fixed, so `alpha / rank` changes from
     2 to 1. Any result measures that combined rank-and-scaling change and must
     not be described as a pure rank effect.
   - Prevention: compare candidates by their effective arm configuration, not
     by variant ID or prose metadata. Let PRAXIST's duplicate handling resolve
     the second proposal, record the actual disposition, and retain 16/32 as a
     distinct later hypothesis if 16/16 does not improve strict critical-value
     exactness. Do not open the final-test split during this iteration.
   - Verified disposition: peer 1 completed the 96/12 canary once; peer 0 then
     emitted `duplicate_cache_hit` and reused that exact summary without a
     second Windows training process. Pair score was 0.2624 and LFM2.5 led
     0.4067 to 0.1181, but both arms preserved all critical values in 0/12
     examples. This is only a routing signal: coverage is 0.06 and the result
     is explicitly partial and promotion-ineligible. It cannot establish that
     rank/alpha 16/16 improves full-corpus literal fidelity.
   - On the identical 96/12 examples and LFM seed, the earlier rank/alpha 8/16
     canary scored 0.4189 with critical-token F1 0.2048; 16/16 scored 0.4067
     with critical-token F1 0.1565. Character similarity and structure rose
     only slightly, while critical and unit fidelity fell and exact critical
     preservation stayed 0/12. The small canary therefore provides no reason
     to spend a development run on 16/16. This does not reject the distinct
     scale-preserving 16/32 hypothesis.
   - The apparent second scheduler job is therefore not evidence of repeated
     provider or GPU work. Check the evaluator log for `duplicate_cache_hit`
     before interrupting a queued/running duplicate; scheduler state alone is
     too coarse because it also covers a cheap cache-resolution process.

26. **The final test now has a simple task-wide one-shot gate**
   - `fresh_windows_350m_operator/final_evaluate_frozen_winner.py` has separate
     `freeze` and `execute` commands. Freeze accepts only a full protocol-v2
     complete result and binds the selected adapter directory, local base
     snapshot, effective config, complete summary, prompt, and current
     `train_pair.py` by SHA-256. It imports no retired training code.
   - Execute uses the fixed `data/test.jsonl` path and requires the exact phrase
     `OPEN_THE_FINAL_TEST_EXACTLY_ONCE`. Before the first manifest/binding
     content read or any test hash/JSONL read, it atomically creates the task-wide
     `scratch/final_test_state/final_test_attempt.json`. Either that claim or a
     completed receipt blocks every later CLI run, even after failure or with a
     different manifest/output path. Tests inject temporary fake rows; the real
     final split has not been opened by this harness.
   - Initial review found two avoidable weak spots: a summary could have carried
     only complete-looking labels without full 1,600/200 coverage evidence, and
     model files were hash-checked only before evaluation. The harness now
     requires all complete, coverage, effort, promotion, and 400-unit fields;
     verifies adapter rank/alpha/dropout against the selected config; and
     re-hashes the manifest, adapter, base, summary, prompt, and evaluator after
     generation before it writes a result or receipt.
   - Independent review then found two pre-claim bypasses in the first draft:
     a caller could point `--manifest` or `--evaluation-summary` at the reserved
     test file and make generic JSON/hash validation open it, and the injectable
     Python function exposed a caller-selected state root. Fix: all freeze and
     manifest bindings now reject path, symlink, or hardlink aliases of the
     reserved test before content access; a normal execution writes its claim
     before validating any manifest or bound-file contents; and the public
     `execute_once` API exposes neither test path nor state root. Fake-path/state
     injection remains private and is used only by isolated tests. A second
     review caught that Python callers could still invoke that private seam
     directly: it now detects the real test by file identity and then requires
     the default task state, production evaluator, and exactly 200 rows.
   - A complete-looking single-arm fixture also passed the first draft. Freeze
     now requires exactly Boldt and LFM2.5, 1,600/200 coverage for each, valid
     finite/ranged arm metrics, matching aggregate scores, both valid adapter
     configs, complete effective configs, and all fresh-corpus/sequential-GPU
     integrity flags. The real Generation-0 complete summary passed these
     structural checks at this stage; item 29 later adds mandatory score-time
     provenance that this older summary intentionally lacks, so it can no
     longer be frozen. `praxist_task/AGENTS.md` now states the narrow one-shot
     final reader exception; iterative reads remain owned by the primary
     evaluator.
   - Verification: all 14 focused fake-data tests pass, including claim-before-
     read, rerun denial after success and failure, partial-summary rejection,
     LoRA-config mismatch rejection, reserved-test alias rejection, fixed public
     and private real-test execution paths, paired-summary enforcement, and
     mutation-during-evaluation detection.
     Both files compile and `git diff --check` passes. This is readiness evidence
     only; freeze and real final execution wait for one PRAXIST winner.
   - Remaining provenance limit: existing `train_pair.py` complete summaries
     name adapter paths and model IDs but do not record the adapter/base/prompt/
     evaluator hashes at the moment the score was produced. Freeze binds their
     current bytes and revalidates them, which detects later mutation only after
     freeze. After PRAXIST selects a configuration, create one frozen-config
     complete rerun whose producer records those hashes before consuming the
     final split; do not claim stronger provenance from the earlier summaries.

27. **Boldt rank/alpha 32/16 does not earn a larger run**
   - Generation 1 changed only Boldt from rank/alpha 16/16 to 32/16 and kept
     LFM2.5 at 8/16. On the identical 96/12 canary, Boldt scored 0.1171 versus
     0.1181 at 16/16. Critical-token F1 stayed 0.0096 and exact critical-value
     preservation stayed 0/12; unit F1 fell from 0.2194 to 0.1778. A small
     character-similarity rise did not compensate for worse fidelity.
   - Consequence: do not advance Boldt 32/16 to development. As with the LFM
     16/16 test, fixed alpha means rank and `alpha / rank` changed together, so
     this is not a pure capacity result. The full-corpus evidence still leaves
     LFM2.5 as the only credible product candidate.
   - The unchanged LFM 8/16 control was retrained because duplicate reuse is
     pair-level, not arm-level. It scored 0.4123 here versus 0.4189 in the first
     canary despite the same config and declared seed; critical F1 was 0.1863
     versus 0.2048. This exposes small CUDA/order nondeterminism and makes tiny
     pair-score differences unsuitable for selection. Preserve exact full-pair
     cache hits now; after the active PRAXIST run, either add deterministic CUDA
     settings plus an arm-level cache or avoid repeating unchanged control arms.

28. **PRAXIST maturity admission can force a full ablation after a negative canary**
   - When the Boldt 32/16 canary finished, Generation 1 was already in
     assessment. The scheduler rejected a development launch because ordinary
     work was closed, while the generation still owed one mature result. The
     peer therefore launched the same candidate directly at complete stage.
   - This is scheduler/protocol completion, not evidence that the canary earned
     promotion. Keep the negative canary interpretation from item 27. Treat the
     1,600/200 run as a full Boldt-rank ablation and as a second independent
     LFM 8/16 reproducibility measurement. Do not let its automatic mature
     status override strict critical-value comparisons.
   - Prevention for a later PRAXIST task: start the final distinct canary early
     enough to permit a development decision before assessment, or predeclare
   a justified mature top-up. Never label an assessment-driven complete run
   as evidence-led promotion.

29. **A one-shot final test needs one canonical ledger, score-time provenance, and one data read**
   - Independent review found four remaining bypasses before the final split
     was opened. First, the task-wide ledger lived beside the runner, so a
     copied runner could obtain a second state directory. Second, a caller
     could place the output directory inside the frozen adapter or base model.
     Third, the test hash and evaluated JSONL rows came from separate file
     opens. Fourth, earlier complete summaries named the adapter but did not
     prove the exact base snapshot, prompt, evaluator, and adapter bytes that
     produced their validation score.
   - Fix: the real-test ledger and output now live at fixed canonical task-root
     paths, and a noncanonical runner is rejected. Output beneath any frozen
     model artifact is rejected before the test read. The final JSONL is read
     into one byte snapshot after the claim, and both its SHA-256 and parsed
     rows derive from those same bytes. A freeze now requires score-time
     bindings for adapter, base snapshot, prompt, evaluator, and effective
     configuration and compares every one with the current bytes.
   - Consequence: existing Generation-0/1 summaries remain valid development
     evidence but are intentionally no longer eligible to freeze for the final
     test. Once PRAXIST selects one configuration, run exactly one complete
     frozen-configuration reproduction that records these bindings at scoring
     time. Only that reproduction may become the final-test manifest.
   - Crash handling is now fail closed. The state directory is prepared before
     manifest publication and is never recreated during final use. Claim files
     use exclusive Windows `CREATE_NEW` plus `FILE_FLAG_WRITE_THROUGH` and file
     `fsync`; POSIX also `fsync`s the parent directory. A failed sync leaves a
     claim artifact in place, so no later attempt can silently reopen the test.
     Storage hardware can still limit absolute power-loss guarantees, but this
     uses the strongest simple local NTFS/POSIX contract available here.
   - Verification: 20 focused fake-data tests pass, including copied-runner,
     alternate-base, missing-provenance, model-output-overlap, sync-failure,
     missing-state-root, and all earlier one-shot cases. Both files compile and
     `git diff --check` passes. The real `data/test.jsonl` remains unopened.

30. **Run ML harness tests with the task environment, not Scriber's general venv**
   - The first post-change unittest command used the repository `.venv`. That
     environment intentionally lacks PyTorch, so importing `train_pair.py`
     failed with `ModuleNotFoundError: torch` before any test ran. This was an
     interpreter-selection error, not a harness regression.
   - Fix and prevention: use
     `praxist_task/fresh_windows_350m/.venv-win/Scripts/python.exe` for every
     training/evaluation harness test. For normal Scriber pytest runs, use the
     repository `venv/Scripts/python.exe`: the separate repository `.venv`
     currently has neither PyTorch nor pytest. Do not install into either
     environment merely to blur these roles. The corrected harness run
     completed all 34 focused tests, and the normal Scriber environment later
     completed all 120 targeted local-polishing/runtime/route tests.

31. **Capture score-time provenance with a wrapper, without changing an active trainer**
   - Modifying `train_pair.py` while PRAXIST is executing it would invalidate
     the running experiment and its later comparisons. A separate
      `fresh_windows_350m_operator/reproduce_frozen_complete.py` now owns the
      single post-selection complete reproduction instead. It invokes only the
      public `evaluations/primary/run.py` boundary, which runs the unchanged
      trainer on train/validation, forces both Hugging Face loads to exact
      cached commit revisions in offline mode, and keeps the arms sequential on
      one GPU.
   - The wrapper hashes prompt, evaluator, and both base snapshots before the
     run and verifies the same bindings afterwards. It intercepts the normal
     validation call only to hash each already-saved adapter immediately before
     and after scoring; any mutation aborts. It then adds the two exact
     `score_provenance` records to the complete summary. This supplies the
     evidence required by item 29 without touching the held-out test.
   - A Windows test initially compared a resolved long path with the equivalent
     8.3 short path (`Alexander.Immler` versus `ALEXAN~1`) as strings and failed.
     Fix: use file identity (`samefile`) for path equivalence and reserve string
     equality for the deliberately canonical runner location. This distinction
     prevents false failures without weakening the copied-runner guard.
   - Verification: six focused reproduction-wrapper tests plus the 20 one-shot
     final-harness tests pass together; all four Python files compile and
     `git diff --check` passes. The wrapper has not been run on a candidate yet;
     that happens exactly once after PRAXIST freezes the winning configuration.

32. **Quality winner and redistributable product winner may differ by license**
   - The official Boldt model card identifies `Boldt/Boldt-DC-350M` as
     Apache-2.0. The official LFM2.5 repository instead ships the LFM Open
     License v1.0. Its redistribution terms require a copy of the license,
     prominent modification notices, and retained attribution/NOTICE material.
     Its commercial-use grant also excludes legal entities at or above the
     stated USD 10 million annual-revenue threshold unless separately licensed.
   - Sources checked on 2026-09-01:
     `https://huggingface.co/Boldt/Boldt-DC-350M` and
     `https://huggingface.co/LiquidAI/LFM2.5-350M-Base/blob/main/LICENSE`.
     This is an engineering distribution constraint, not a legal conclusion
     about the user's entity; revenue/licensing eligibility is still an
     external fact that source code cannot establish.
   - Consequence: keep PRAXIST's quality comparison unchanged, but do not
     publish or ship an LFM-derived GGUF under the old Google/Gemma notices.
     If LFM2.5 remains the quality winner, package its exact license and
     modification notice and confirm commercial eligibility before public
     redistribution. A local-only validation can proceed without pretending
     that this product-release question has already been resolved. Boldt does
     not carry this particular threshold and remains the permissive fallback.

33. **Randomize both split membership and row order; never take an ascending-number cut**
   - The source example number is not used to assign a contiguous train,
     validation, or final-test block. Seed `3502026` first reserves the final
     test by complete canonical topic family, then searches 10,000 seeded
     candidates for a 1,600/200 public split balanced across topics, all error
     profiles, all error tags, list shape, and all eight 250-number bands.
   - After membership is fixed, every split is independently shuffled before
     JSONL output. The first training indices are `1543, 71, 1399, 905, ...`;
     validation starts `546, 1846, 759, 1838, ...`. Training contains 195-203
     examples from each number band and validation 22-30, so neither file is an
     ascending error-case block. The untouched final test contains exactly 25
     examples from every band.
   - The trainer additionally uses a seeded shuffled `DataLoader` for each arm.
     This matters because canary and development stages consume prefixes of the
     already shuffled train/validation files; without output shuffling those
     smaller stages could still see ordered error clusters even when the full
     membership split was balanced.
   - Prevention: keep the shuffled-index and per-band audit in
     `data/dataset_stats.json`, and treat any regenerated split lacking those
     checks as ineligible for comparison with the current PRAXIST run.

34. **A one-shot boundary needs shared bindings, exact module loading, and an external ledger anchor**
   - A follow-up review found that the reproduction wrapper and freezer had
     encoded directory sizes and digests differently. The same adapter would
     therefore have been rejected at freeze even though both individual test
     suites passed. Fix: the reproduction wrapper now delegates file and
     directory bindings to the final harness, and a cross-component test sends
     producer provenance directly through `freeze_winner_manifest()`.
   - Adding the task root to `sys.path` did not bind a concrete Python module:
     an already loaded `train_pair` or `render_dashboard` could win before its
     path was checked. Fix: the harness loads both expected source files with
     `spec_from_file_location()` under private names, temporarily supplies only
     the canonical dashboard dependency, and verifies file identity. A test
     places executable shadows both on `sys.path` and in `sys.modules` and
     confirms that neither shadow runs.
   - A ledger below a discovered task root moves with a copied task tree. Fix:
     the real create-once ledger is anchored outside the task under the current
     account's OS-resolved local Scriber state directory. The output remains at
     its fixed task path, but every copy shares the one ledger name on this
     machine and account. This is a local exactly-once boundary, not a claim
     across other computers or deliberately modified source code.
   - Directory metadata must be committed before the irreversible read too.
     POSIX now creates the state/output directory and fsyncs its parent;
     Windows creates a same-parent staging directory and publishes it with
     `MoveFileExW(MOVEFILE_WRITE_THROUGH)`. A directory-publication failure is
     tested to leave the claim in place while the data reader remains unused.
     Reference: `https://learn.microsoft.com/en-us/windows/win32/api/winbase/nf-winbase-movefileexa`.
   - Verification: 34 focused fake-data tests pass, including direct
     producer-to-freezer acceptance, shadow modules, external ledger anchoring,
     and claim -> durable output -> first-read ordering. Four files compile,
     `git diff --check` passes, and the real `data/test.jsonl` remains unopened.
     An independent follow-up audit found no remaining defect in the four
     reviewed boundaries.

35. **An installation directory alone does not mean an old local model is installed**
   - The installed Scriber data root contains the old Gemma revision directory
     with `q8_0` and `bf16` subdirectories, but both are empty. There is no
     current pointer or installation manifest and therefore no old GGUF payload
     to preserve or benchmark on this machine.
   - The manager correctly treats an installation as usable only through its
     pointer, manifest, and complete verified artifact set. Product migration
     must use that same semantic check rather than infer readiness from a stale
     directory name.
   - Consequence: after the new winner passes install, prewarm, and live-polish
     checks, cleanup can remove these exact empty legacy directories and the old
     catalog/UI references. It must still resolve and verify the narrow paths
     immediately before deletion; no broad models-root cleanup is appropriate.

36. **Doubling Boldt LoRA rank to 32 is not an efficient quality improvement**
   - On the complete 1,600/200 public split, Boldt rank 32/alpha 16 scored
     0.7847 versus 0.7833 for rank 16/alpha 16. Strict critical-value
     preservation rose only from 17/200 to 19/200, while critical-value F1
     slipped from 0.7843 to 0.7839 and character similarity from 0.9904 to
     0.9899. Exact full-output matches rose from 15/200 to 17/200.
   - The adapter doubled its trainable parameters from 9.04 million to 18.09
     million. Median generation latency worsened from 6.91 to 8.64 seconds,
     p95 from 9.01 to 10.11 seconds, complete-arm time from 1,754 to 2,409
     seconds, and peak VRAM from 2.50 to 2.66 GB. Unit exactness improved from
     188/200 to 190/200, but that small gain does not offset the cost and mixed
     critical-value result.
   - Consequence: the earlier negative rank-32 canary correctly predicted that
     capacity alone was not the limiting factor. Keep rank 16 as Boldt's better
     efficiency point unless a later hypothesis changes optimization exposure
     rather than only adapter size. The running LFM2.5 control arm still has to
     finish before this forced complete pair is closed.

37. **The ordinary language-model loss underweights the exact-value objective**
   - A tokenizer-offset audit over the 1,600 training and 200 development rows
     opened only `train.jsonl` and `validation.jsonl`; the final test remained
     untouched. Every audited row contains at least one critical target span.
   - For Boldt, critical spans occupy 66,705 of 386,111 target tokens (17.28%,
     37.06 of 214.51 tokens per row on average). For LFM2.5 they occupy 65,982
     of 478,220 target tokens (13.80%, 36.66 of 265.68 tokens per row).
   - Consequence: the current uniform causal-language-model loss can become
     very small while most of its gradient still rewards ordinary prose rather
     than exact numbers, dates, times, units, and legal citations. After the
     fixed PRAXIST run closes, a bounded critical-token loss-weight experiment
     is a stronger next hypothesis than another LoRA-rank increase. It must be
     evaluated on the same public validation split before any final-test use.

38. **The repeated LFM2.5 control confirms the leader, not a new improvement**
   - Generation 1 repeated the unchanged LFM2.5 rank-8/alpha-16 arm with the
     same 1,600/200 split and seed while only Boldt's rank changed. It scored
     0.9034 versus 0.9014 in generation 0. Critical-value F1 moved from 0.9315
     to 0.9329 and exact critical preservation from 101/200 to 103/200;
     character similarity stayed effectively flat at 0.9991, unit exactness
     stayed 199/200, and structure stayed 0.9998.
   - Median/p95 generation latency was 7.07/8.84 seconds versus 7.00/8.46
     seconds in generation 0. Mean training loss was 0.0718 versus 0.0719 and
     complete-arm time 1,957 versus 1,980 seconds. The two-row exactness change
     is therefore run variance, not evidence of a configuration improvement.
   - The full generation-1 pair scored 0.8441 and again selected LFM2.5 over
     Boldt rank 32 (0.9034 versus 0.7847). Protocol integrity, 400/400
     evaluation units, and complete public-validation coverage all passed.
   - Consequence: freeze LFM2.5 rank 8 as the stable raw-quality frontier for
     comparison, reject the rank-only branches, and spend the remaining public
     research budget on the literal-value objective rather than claim the
     repeated control's 1 percentage point as progress. The final test remains
     unopened.

39. **A paired aggregate can promote the changed arm for the wrong reason**
   - PRAXIST formally promoted `fresh-boldt-r32-gen1-peer0` because its pair
     score was 0.8441 versus generation 0's 0.8424. The only intentionally
     changed arm, Boldt, did not justify that promotion: rank 32 lost critical
     F1 and speed while gaining only 2/200 exact rows at twice the trainable
     parameter count.
   - The pair increase instead came from the unchanged LFM2.5 control moving
     from 101/200 to 103/200 exact critical preservation under the same config
     and seed. That small repeat-run variance was incorrectly attributed to
     the Boldt-rank intervention by the pair-level frontier metric.
   - Consequence: never infer an arm-specific causal improvement from the pair
     score alone. Compare the changed arm against its own fixed control and
     treat unchanged-arm variance separately. Generation-2 proposals may use
     the formal frontier as research context, but final configuration selection
     must reject rank 32 unless direct Boldt evidence reverses this result.

40. **A plausible PI agenda can still be rejected by PRAXIST's role contract**
   - The generation-2 synthesis produced useful optimizer and fixed-rank
     hypotheses, but assigned only the peer roles `bridge` and `anti_mainline`.
     PRAXIST 0.5.0 requires explicit `exploit` and `falsifier` roles for this
     agenda shape, so it stored the proposal as
     `research_agenda_gen2.yaml.rejected` with the exact validation error
     `peer_contracts missing required roles: ['exploit', 'falsifier']`.
   - PRAXIST recovered automatically by starting both generation-2 peers under
     its frontier-driven Free Explore fallback. No GPU job or result was lost,
     and the rejected agenda remains auditable rather than silently accepted.
   - Consequence: treat agenda generation and agenda-schema admission as two
     separate gates. A future task prompt should require the mandatory role
     vocabulary explicitly. For this fixed run, monitor Free Explore proposals
     against the arm-specific evidence and do not rewrite the live controller.

41. **Compare normalized arm configs before calling two queued jobs duplicates**
   - The queued Gen-2 canary was canceled through the scheduler's safe
     `cancel_queued` RPC after its directory name was mistaken for the already
     running complete configuration. The process action itself was atomic and
     did not disturb the running job, but the duplicate classification was
     wrong.
   - Exact `variant.json` inspection shows that peer 0 lowered only Boldt to
     learning rate 0.0001 while keeping LFM2.5 at 0.0002. Peer 1 did the
     opposite: Boldt remained at 0.0002/rank 32 and LFM2.5 changed to 0.0001.
     The peer-1 directory is named `fresh-boldt-r32-lr1e4`, its embedded
     `variant_id` says `fresh-lfm2-r8-lr1e4-gen2-peer1`, and the status view
     surfaced the latter. Neither name alone encodes both arms reliably.
   - The canceled job had zero attempts and therefore produced no model or
     metric. Its terminal `admission_timeout` is still the generic cancellation
     state, not a real timeout, but the missing LFM learning-rate canary must be
     recovered through the normal primary evaluator/scheduler before this
     hypothesis is considered tested.
   - A first recovery with `--retry-terminal` was rejected before admission:
     the queued cancellation left a scheduler record but no retained launched
     experiment eligible for that protected-process retry mode. The safe
     recovery used a new explicit `recovery-canary` tag plus
     `--allow-duplicate`, while preserving the exact variant directory, seed,
     mode, output directory, profile, and primary-evaluator command. The central
     scheduler accepted job `6600f896acef4e8aafd3d38bde60caf1` behind the
     running complete job with attempts/PID still zero, `running=1`,
     `queued=1`, concurrency 1, and Gen 2 still unfrozen. When the mature
     complete result arrived, PRAXIST immediately froze Gen 2 before admitting
     the queued recovery; it ended with
     `generation_closing:mature_quorum`, attempts/PID zero, and no output.
     Therefore the recovery is still unexecuted and must be an explicit first
     canary in the next fresh PRAXIST run, not retroactively attached here.
   - Prevention: establish duplication from the fully normalized two-arm
     configuration, seed, mode, train/validation counts, and evaluator binding;
     never from experiment IDs, directory names, or one arm. Scheduler RPC is
     the right cancellation mechanism only after that semantic identity proof.

42. **A small model can be repaired only where numeric provenance is unambiguous**
   - The validation runs show that the remaining high-risk failures are often
     locally small value substitutions rather than broad language failures. A
     product-side repair is therefore useful, but only when the source and
     generated value have the same unique semantic role, order, unit, and
     component shape.
   - `repair_unambiguous_numeric_anchors()` now handles digit-formatted numbers,
     German decimals, dates, and times. It plans every replacement before
     changing the output, then reparses and proves the complete result again.
     A count, unit, order, role, format, or ambiguity mismatch raises the
     existing safety error and returns the original transcript instead of a
     partially repaired answer.
   - Literal source digits remain protected by the existing placeholder path;
     the repair is deliberately fail-closed when that provenance could be
     mixed with a second correction. This prevents a moved or invented number
     from being legitimized merely because another nearby value is repairable.
   - Verification after the independent edge-case fixes: the combined Local
     Polishing, runtime-packaging, and API-route gate reports 146 passing tests.
     Coverage includes plain-text and rendered-SST paths,
     multi-value roles, invalid dates, changed units, telephone-like digits,
     ambiguity, and all-or-nothing behavior. Ruff, Python compilation, and
     `git diff --check` pass; no final-test row or retired corpus was opened.

43. **Validate first, repair only a typed numeric failure, then validate again**
   - Independent adversarial review found that running the repair before the
     existing validator rejected a legitimate self-correction such as
     `zehn, nein, zwanzig Euro -> 20 €`. It also found that the legacy SST path
     could repair a number while a separate semantic verb change escaped its
     weaker validation.
   - Fix: both rendered output paths first run the strong plain-text semantic
     validator. A repair is attempted only when that gate raises exactly
     `changed_number`; the complete repaired output then passes the same strong
     validator again. SST retains its structural validation as an additional
     gate. A non-numeric failure is never converted into a repair opportunity.
   - The same review found that unconstrained German number-word composition
     treated `zwanzig fünf Euro` as the year 2005. Split 19xx/20xx years now
     require immediate syntactic year or month context, approved connectors,
     and no following unit. Money-like and otherwise ambiguous sequences fail
     closed, while an explicit phrase such as `Das Jahr ist zwanzig fünf`
     remains eligible.
   - Prevention: make repair code an exception handler around a stronger
     acceptance proof, not a substitute for that proof; constrain compressed
     number-word grammars with syntax and units rather than numeric shape alone.
     The focused three-suite gate passes all 146 tests after these regressions
     were added.

44. **Halving Boldt's learning rate undertrains the fixed one-epoch budget**
   - Generation 2 kept Boldt at rank 32 but lowered its learning rate from
     0.0002 to 0.0001. On the complete 1,600/200 public split its score fell
     from 0.7847 to 0.7034. Critical-value F1 fell from 0.7839 to 0.6415 and
     exact critical preservation from 19/200 to 0/200; character similarity
     fell from 0.9899 to 0.9629, unit exactness from 190/200 to 161/200, and
     structure from 0.9981 to 0.9495.
   - Mean/last training loss rose from 0.1959/0.0430 to 0.2890/0.0945. Median
     and p95 generation latency also worsened from 8.64/10.11 seconds to
     9.19/11.39 seconds. This is not a fidelity trade-off: every measured
     quality dimension became worse while the one-epoch step count stayed
     fixed.
   - Consequence: reject 0.0001 for Boldt under the current exposure. Retain
     rank 16 at 0.0002 as Boldt's efficient comparator, and target the loss
     allocation toward critical tokens rather than reducing overall update
     magnitude. The LFM2.5 control arm must still finish before the paired
     generation closes; the final test remains unopened.

45. **Mask list indices before assigning critical-token loss weight**
   - A read-only offset audit used only the 1,600 training and 200 public
     validation rows with the exact frozen Boldt and LFM2.5 tokenizers. Both
     tokenizers are fast and returned one offset entry per token. The untouched
     final test was never opened and the audit used no GPU.
   - There are 3,580 numbered-list prefixes across 895 rows. The ordinary
     critical-value regex sees all 3,580 indices as numbers; replacing each
     prefix with same-length spaces before span extraction removes exactly
     those 3,580 false critical tokens for each tokenizer without moving any
     later offset. Deleting the prefix would corrupt every following offset.
   - After masking, Boldt has 63,125 weighted tokens out of 386,111 target/EOS
     tokens (16.35%); LFM2.5 has 62,402 out of 478,220 (13.05%). Every one of
     the 1,800 public rows still has at least one real critical token. At weight
     2, critical tokens carry 28.10%/23.09% of normalized Boldt/LFM loss mass;
     at weight 4, they carry 43.88%/37.51%. Weight 4 is therefore a genuinely
     aggressive falsifier rather than a small adjustment.
   - Each row has exactly one appended EOS-region token. Although both fast
     tokenizers return a real offset for that appended text, the span search is
     bounded to the original target, so all 1,800 EOS tokens remain at normal
     weight 1. Prompt/BOS and padding must remain weight 0.
   - Consequence: test only the bounded same-seed weights 2 and 4 against the
     existing weight-1 control, compare each arm independently, and compose at
     most one asymmetric complete candidate from the per-arm winners. Do not
     run a 3-by-3 factorial search or promote weight 4 without a critical
     exact/F1 gain and preserved character, unit, and structure quality.

46. **Re-run every claimed gate after the final review patch**
   - The first numeric-repair implementation passed Ruff, but its later
     adversarial fixes introduced one new `SIM108` finding in the split-year
     branch. The earlier clean result no longer described the final bytes.
   - Fix: express the two proven number-composition branches as the equivalent
     conditional expression, then rerun both Ruff and the full focused product
     gate. Ruff is clean and all 146 Local Polishing, packaging, and API-route
     tests still pass.
   - Prevention: report a validation result only for the exact final file
     state; any subsequent patch invalidates the previous lint/test claim even
     when the change appears mechanical.

47. **Public does not mean that an ad-hoc corpus reader is permitted**
   - The tokenizer-offset audit in learning 45 opened only the public
     train/validation files and never touched the final split, but it did so
     through an ad-hoc read-only audit rather than through the task's required
     `evaluations/primary/run.py` ownership boundary. That violates the local
     research-sandbox access contract even though the measured rows themselves
     were eligible for iterative work.
   - Consequence: retain the audit only as implementation-debugging context;
     do not use its numbers as selection evidence until the weighted trainer
     emits the same masked-span/token counts through the normal primary
     evaluator and bound result summary. The next patch will make active
     weighting fail closed when it observes zero weighted tokens and will
     record its aggregate counts in the ordinary evaluation artifact.
   - Prevention: every corpus read, including a harmless tokenizer statistic,
     must be initiated by the primary evaluator or the single-use final reader.
     A separate script may use injected fake rows in tests but must never open
     a real split directly. Before delegating data analysis, include this
     ownership rule explicitly rather than stating only `do not open test`.

48. **The training and product prompts match, but raw validation does not prove placeholder copying**
   - The task prompt is 310 UTF-8 bytes with SHA-256
     `372f879803334a68e310fe2e658c11678600baf0f4ef72834e4acd409f747dd6`.
     Replacing only its literal `${output}` slot with the product's
     `${transcript}` slot produces the exact 314-byte product template and
     already pinned SHA-256
     `e0ff2d5297f3d4d5ae7b8af85ea1cf52a24704bfb2e61990eab6de52b42058d8`.
     After rendering a source string, the model therefore receives the same
     prompt bytes; the placeholder variable name is not a training/product
     mismatch.
   - The public evaluator nevertheless feeds raw source text to the adapters.
     It does not execute the product's protection policy, and that policy plus
     its implementation are intentionally outside the research sandbox. Raw
     critical-value metrics therefore do not prove that a candidate copies,
     preserves, and restores every `KEEP` placeholder in the installed path.
   - Consequence: keep model selection on the bound public metric, then require
     an actual product-path placeholder smoke after exporting the selected Q8
     and BF16 artifacts. Missing, additional, reordered, duplicated, unknown,
     or residual placeholders must cause original-text fallback, never an
     accepted partial output. Repeat the same gate against the real Q8 GGUF so
     tokenizer and stop behavior are covered, not only the HF adapter.
   - If and only if the real gate fails because the model cannot preserve the
     markers, create at most 400 deterministic placeholderized views from the
     same 1,600 fresh training rows while retaining every original row. Each
     transformed target must restore byte-for-byte to its original target; no
     old corpus or synthetic semantic content may enter through this repair.

49. **Twelve validation rows can reject a collapse, not rank nearby configurations**
   - A metadata-only review found that Gen-1 canaries preserved the obvious
     LFM2.5-over-Boldt model ordering, but failed to resolve the actual
     configuration question. Boldt rank 16 scored 0.1181 versus rank 32 at
     0.1171 in canary, while complete results reversed that tiny score ordering
     to 0.7833 versus 0.7847. Critical F1 tied in canary, and all six active v2
     canary arm cells had 0/12 exact critical examples.
   - Unchanged LFM2.5 rank-8 canaries varied by 0.0066 score and 0.0185 critical
     F1. That noise was as large as the rank intervention being studied. Even
     complete repetitions moved from 101/200 to 103/200 exact critical rows;
     58.6% of the apparent Gen-0-to-Gen-1 pair-score increase came from that
     unchanged control rather than the changed Boldt arm.
   - The 600/60 development stage was materially more informative: it exposed
     5/60 exact LFM critical rows versus 0/60 for Boldt and preserved the later
     complete ordering across score and critical F1. It is therefore the
     smallest existing stage with useful resolution for the loss-weight
     choice.
   - Consequence: run weights 2 and 4 through canary only as crash/fidelity
     screens, then run both surviving weights through development. Select per
     arm using development critical exact/F1 with character, unit, and
     structure guardrails, and execute exactly one mixed complete candidate.
     Never choose the complete configuration from a 12-row pair-score margin.

50. **A third unchanged LFM2.5 complete run defines the control-variance band**
   - The Gen-2 LFM2.5 rank-8/learning-rate-0.0002 control scored 0.90254 with
     critical F1 0.93250 and exact critical preservation 102/200. Its prior two
     identical complete runs scored 0.90144/101 rows and 0.90342/103 rows. The
     new result lands almost exactly between them and confirms that a one- or
     two-row movement is ordinary repeat variance, not a configuration gain.
   - Character similarity was 0.99906, unit exactness 199/200, structure
     0.99981, median/p95 generation latency 7.20/8.67 seconds, and mean/last
     training loss 0.07185/0.00910. These also remain within the earlier
     control range.
   - The complete pair score fell to 0.80299 only because the changed Boldt
     arm collapsed to 0.70343; LFM remained the winner at 0.90254. Selection
     must therefore retain LFM rank 8/0.0002 as the weight-1 control and reject
     the Boldt 0.0001 intervention arm-specifically, independent of any formal
     paired frontier decision.
   - The canceled LFM-0.0001 canary still has no evidence: its queued recovery
     was closed with Gen 2 before attempt zero could start. The next PRAXIST run
     must execute that exact recovery canary before the new weight-4 falsifier.

51. **PRAXIST promotion is not a substitute for a task-specific acceptance baseline**
   - The Gen-2 boundary promoted one 0.8030 pair finding even though the prior
     complete frontier pair scored 0.8441 and the changed Boldt arm clearly
     regressed. The launcher repeatedly warned that `task.yaml` defined no
     baselines, so every result reporting the primary pair score was treated as
     above baseline and the lane selected a candidate within the generation.
   - The diversity report also labeled the learning-rate experiment a 6/6
     dimensional clone of the rank-32 parent. Its copied
     `design_dimensions` still described only the old rank intervention and
     never encoded the actual optimizer change. The exact arm configuration
     proved a change, while the research labels hid it.
   - Consequence: reject the formal Gen-2 promotion for model selection. The
     next task spec must bind explicit score/critical/structure baselines, every
     variant must state its real loss-weight or learning-rate intervention, and
     final selection must compare each changed arm against its own control.
     PRAXIST remains the scheduler, provenance store, and visualization layer;
   the product acceptance rule remains stricter than its generic frontier.

52. **Generation and peer limits do not bound the number of evaluations**
   - A read-only audit of the completed PRAXIST run shows that
     `max_generations: 1` and `cohort_size: 2` constrain only the generation
     topology. One peer may still submit several canary, development, complete,
     retry, or duplicate evaluations. The observed peer contracts were empty
     and the role assessment was explicitly `advisory_only`, so a Jinja role
     assignment is guidance rather than an enforcement boundary.
   - Consequence: the next run must state the complete permitted sequence
     explicitly. The two loss-weight arms may each pass their crash-screen
     canary and exactly one development evaluation; the previously canceled
     LFM learning-rate canary is a separate required recovery check. Only the
     coordinator may start a complete evaluation, only after both canonical
     development summaries exist, and it may do so exactly once. The final
     inventory must match those intended stages rather than infer correctness
     from the generation count.
   - `task_assets.baseline_variant` did not create a metric baseline. A real
     baseline must be declared in top-level `baselines`, but the exact nonempty
     YAML form is not yet proven by an allowed local example. Treat the proposed
     `name`/`value` record as provisional until `praxist resolve` retains it and
     the launched run reports one cached baseline without the previous
     `No baselines defined` warning. A global pair-score baseline still cannot
     enforce the arm-specific critical-exact/F1, character, unit, and structure
     gates; those remain explicit selection rules.
   - Disable generic quality-diversity steering for this bounded falsification:
     weights 2 and 4 intentionally alter the same design dimension, so clone
     rejection would work against the experiment rather than protect it. Also
     neutralize or explicitly set `MAX_GENERATIONS` and `COHORT_SIZE` at launch,
     because environment overrides can silently replace the checked task spec.

53. **Use PRAXIST's normalized metric fields for a real baseline**
   - The plausible `baselines[].value` spelling was accepted syntactically by
     the installed PRAXIST 0.5.0 loader but silently normalized to `0.0`. That
     would make every nonnegative pair score look above baseline and repeat the
     earlier false-promotion problem under a less obvious configuration.
   - The supported record uses `metric_name: pair_score`,
     `metric_value: 0.6914067426734694`, and `direction: maximize`. Both the
     direct loader and a minimized task-local `praxist resolve` preserve the
     exact measured W1 Development value together with one generation and two
     peers.
   - Prevention: never treat schema acceptance as value preservation. Inspect
     the normalized effective task and assert the exact numeric baseline before
     launch; after launch also require one cached baseline and no
     `No baselines defined` warning.

54. **A per-peer order does not establish a global scheduler order**
   - The first weighted-loss prompt told peer 1 to run the canceled LFM
     learning-rate recovery before its own W4 job, but peer 0 was independently
     allowed to submit W2 immediately. With concurrent peers and one central
     GPU queue, W2 could therefore be admitted before the recovery even though
     both agents followed their local instructions.
   - Fix: the recovery job is now a generation-wide admission barrier. Peer 1
     must submit it first and publish its terminal scheduler job ID/status;
     peer 0 may not create or submit W2 until that terminal barrier exists and
     no recovery job remains queued or running. W4 is bound to the same barrier.
   - Prevention: express cross-peer sequencing as an observable shared barrier,
     not as two locally ordered bullet lists. A directory or proposed variant
     does not prove that a scheduler job ran or reached terminal state.

55. **Generated files inside the task root make the research manifest drift**
   - A real PRAXIST resolve proved that `.gitignore` is not the task-manifest
     exclusion boundary: `.ruff_cache`, `dashboard/latest.*`, and every
     `scratch/*` operator/finalization script entered the manifest. Declaring
     `scratch` writable additionally made the final-test and reproduction
     harnesses peer-mutable.
   - Fix: remove task-root scratch write authority, keep private
     finalization tools outside the research task project, delete only the
     disposable cache/render outputs after exact-path verification, and write
     future stable dashboards below the immutable run directory instead of the
     task source. The old generated outputs were moved to a recoverable archive,
     not deleted. The clean task-local resolve now contains 23 immutable source
     files with manifest SHA-256
     `6d67a34d2565776cbcd2227797a46050ad6286e2d6c6dbfee93a7638882f78be`;
     `scratch`, `.ruff_cache`, dashboard outputs, operator tools, all three data
     splits, experiments, and `.venv-win` are absent.
   - Prevention: inspect `task_project_manifest.json` itself before every new
     run. Source-control ignore rules and runtime write rules have different
     purposes and cannot be assumed to define PRAXIST's hash surface.

56. **Freeze and reproduction must preserve the same evaluator boundary as training**
   - Independent review found two post-selection gaps. The final freezer bound
     adapter/config/metrics but did not prove that a W2/W4 Complete actually
     reported active offset-based weighting with positive critical target
     tokens. The reproduction wrapper also called `train_pair.run()` directly,
     bypassing the sole public corpus-reader boundary.
   - Fix: freezing now requires exact top-level/arm aggregate equality and the
     same weight-bound schema used by the primary evaluator: 1,600 examples per
     arm; W1 inactive with no offsets and null weighted-token count; W2/W4
     active with offsets, positive spans, and positive critical target tokens.
     Missing, manipulated, or contradictory aggregates fail closed. The one
     exact reproduction now calls only `evaluations/primary/run.py::run`, whose
     canonical Complete invocation is covered by fake-data tests.
   - Offline reproduction binds both the explicitly requested Hugging Face
     revision and the local default revision to the same pinned cache commit.
     If the default ref resolves elsewhere, the run stops before any corpus
     read rather than silently loading different base bytes.
   - Verification on the relocated operator harness: 51/51 export, freeze, and
     reproduction tests pass; the 19 core metric/loss/cache tests also pass.
     No real JSONL or final-test row was opened, and no GPU experiment started.

57. **Bind scheduler work class to evaluation stage and distinguish caps from quotas**
   - The first fixed-run prompt left `--work-class=<scout|ordinary|mature>` as
     a peer choice. A wrong class could distort scheduler and maturity behavior
     even when the evaluator stage itself was correct. It also said there were
     "exactly" five preliminary jobs and one Complete while separately requiring
     a failed branch to stop, which could invite a replacement job merely to
     satisfy the count.
   - Fix: Canary is now always `scout`, Development always `ordinary`, and
     Complete always `mature`. Five preliminary plus one Complete describes only
     the successful path; every path has those numbers as hard maxima, and a
     failed prerequisite produces fewer jobs with no retry under a new tag.
   - The final prompt renders for peer 0, peer 1, and an invalid role; the real
     PRAXIST 0.5.0 resolve retains max-generations 1, cohort 2, QD disabled, and
     the exact W1 baseline. `praxist doctor` reports the task/runtime ready; its
     only warnings are the intentionally non-PATH console and the provider-auth
     probe deferred to the explicit Codex-native start.

58. **A declared comparison baseline and a curated baseline asset are separate bindings**
   - The normalized top-level `baselines` record already supplied PRAXIST's
     comparison threshold, but `task_assets.baseline_variant` alone did not
     make the existing W1 result discoverable as curated evidence. Merely
     placing conventional summary and `results.jsonl` files below
     `assets/baselines` was also insufficient. Two launch probes exposed this;
     both were stopped before any scheduler evaluator job was admitted. The
     second probe had only created the intended Recovery variant.
   - Fix: bind the same immutable file explicitly as both
     `task_assets.baseline_results` and
     `task_assets.baselines.curated_results`, correct the task description so
     Peer 1 owns Recovery exactly as the rendered prompt does, and bump the
     task to version 0.3.2. A clean resolve now contains 27 immutable files with
     manifest SHA-256
     `0707b82b91bb3fd50529b83a2204a430f93f4efc2916b698822c9e96a53908d7`.
   - The successful run reports `0 fresh, 1 curated, 0 stale, 0 missing` and
     both peers receive the exact W1 pair-score threshold
     `0.6914067426734694`. `missing_runtime_cache_baselines` only says that the
     current run has not remeasured W1; it is not a missing-baseline error.
     Promotion reads the declared task baseline, while curated evidence
     satisfies availability. Do not forge a fresh cache timestamp for an old
     measurement; no supported curated-to-fresh import exists.
   - The first and only admitted job is now the Peer-1 LFM-LR1e-4 Recovery
     Canary with work class `scout`; the central concurrency limit is one and
     neither W2 nor W4 existed when Recovery began. Prevention: inspect the
     effective task, curated-cache union, rendered roles, and first scheduler
     admission independently instead of treating a successful resolve as proof
     of runtime behavior.

59. **Use the task's existing test runner instead of mutating its training environment**
   - A verification attempt used `python -m pytest`, but the deliberately
     minimal `.venv-win` does not install pytest and answered
     `No module named pytest`. Installing an unrelated runner during an active
     research launch would have changed the training environment without
     improving the tests.
   - Fix: the two focused files are ordinary `unittest` suites, so the same
     frozen task interpreter ran them with `python -m unittest`; all 12
     critical-loss and primary-cache tests passed. Prevention: inspect the test
     framework before adding a package, and keep task-environment changes tied
     to actual trainer requirements.

60. **A shared barrier orders the prerequisite, not the peers released after it**
   - The previously canceled LFM2.5 learning-rate Recovery finally completed
     as the first scheduler job with exit code 0: 96 training and 12 public
     validation rows per arm, 577.85 seconds wall time, and no retry. Its LFM2
     LR-0.0001 arm scored 0.26898 with critical-value F1 0.0 and exact critical
     preservation 0/12; the unchanged Boldt control scored 0.11809 and the pair
     score was 0.19354. This is diagnostic Canary evidence only and cannot
     select a W1/W2/W4 arm.
   - PEFT warned that LFM2's resized embedding layer would be saved with the
     adapter. That follows from the intentional tokenizer resize and the job
     then trained, saved, evaluated, and exited successfully; it is not an
     infrastructure failure. Retain the warning for later merge/export and
     exact-reproduction checks rather than suppressing it during research.
   - Once Recovery became terminal, both peers were legitimately released.
     Peer 1 submitted W4 first and Peer 0 submitted W2 about ten seconds later;
     central concurrency one ran W4 and kept W2 queued. This preserves the
     required global invariant that no weighted job precedes Recovery and each
     peer's Canary-before-Development order. It also proves that a shared
     prerequisite barrier does not impose an order among the peers after
     release. If a future protocol truly requires W2 before W4, it needs a
     second observable W2-terminal barrier rather than a numbered prose list.

61. **PRAXIST's information-density assessment is an admission fence, not a report-only timer**
   - Task version 0.3.2 retained the generic synthesis trigger with two findings,
     one contributing peer, and a 20-minute minimum interval. At exactly that
     boundary those conditions were satisfied even though W4 Canary was still
     running and W2 Canary was waiting behind it. PRAXIST moved Generation 0 to
     Assessment with reason `info_density`, stopped ordinary admission, and
     rejected queued W2 four milliseconds later. W2 had `attempts: 0`: no
     trainer, evaluator log, or summary ever existed, so this was not a model,
     GPU, or weighted-loss failure.
   - The already-running W4 Canary was allowed to finish and proved the real
     weight-4 path: both arm aggregates used offsets and active weighting;
     Boldt had 3,400 critical target tokens and LFM2 had 3,357. Its pair score
     was 0.24375; Boldt/LFM2 critical F1 was 0.03499/0.14358 and both exact
     rates remained 0/12. These Canary values are technical evidence only, not
     configuration-ranking evidence.
   - Same-run recovery is unsupported. Assessment state is replayed on resume,
     there is no public operation to withdraw it, and any retried `scout` or
     `ordinary` job would be rejected again. Mislabeling Development as
     `mature` would evade the scheduler at the cost of invalidating the fixed
     protocol. After W4 evidence was safely written and no GPU work remained,
     the run was deliberately stopped; its later SIGTERM/exit 143 records that
     operator stop and is separate from the earlier W2 rejection.
   - Fix initiated in task version 0.3.3: keep the synthesis trigger enabled so one
     mature Complete can close the generation, but raise `min_findings` to
     1,000, move `max_interval_minutes` close to the eight-hour generation
     horizon, and disable adaptive early assessment. This makes generic
     information-density closure unreachable for the six-job protocol while
     preserving the one-Complete mature quorum. Also remove the duplicated
     `There are On the successful path` prompt wording found during the audit.
     Prevention: calculate trigger time against worst-case queued GPU duration;
     generation/cohort limits alone do not protect ordinary jobs from an
     assessment fence.

62. **Leave a termination gap between the synthesis safety horizon and peer timeout**
   - The first corrected resolve accepted a 480-minute maximum synthesis
     interval but warned that it exactly matched `per_generation_hours: 8`.
     Near-simultaneous peer timeout and orchestrator assessment would leave
     ambiguous, race-prone shutdown semantics even though the ordinary
     information-density trigger was disabled.
   - Fix: task version 0.3.4 sets the safety assessment to 450 minutes. The
     fixed protocol is expected to finish far earlier, while the remaining
     30-minute gap gives PRAXIST a distinct orderly-close window before the
     peer deadline. Prevention: treat a schema-valid resolve warning as an
     actionable boundary check and require the final clean resolve, not merely
     successful parsing, before launching expensive GPU work.
   - The final resolve is warning-free, retains 27 immutable files with
     manifest SHA-256
     `dcead24c3dd9d3171791e661fe4a143fc8c0590dc51040f4f4f40c35cc0153f6`,
     and normalizes exactly 1,000 findings, 450 minutes, adaptive disabled,
     cohort two, one generation, and the W1 baseline. The restarted runtime
     logs `findings=0/1000` with a 450-minute cap, and its first admitted job is
     again the Peer-1 Recovery Canary while Peer 0 waits on the terminal
     barrier.
   - Runtime proof at the former failure point: after 20.5 minutes W2 Canary was
     running, W4 Canary was queued behind it, the trigger logged
     `findings=2/1000`, and the scheduler still reported zero rejected jobs and
     `assessment_generations: []`. This reproduces the prior queue pressure at
     the exact old boundary and confirms the fix under real work rather than
     only through schema inspection.

63. **A fixed seed does not make the CUDA training path bit-identical**
   - The deliberately repeated Recovery Canary used the same immutable task,
     seed, data split, model revisions, optimizer settings, and 96/12 coverage
     as its earlier execution. The first logged losses matched, but the final
     losses differed slightly: Boldt changed from about 0.727678 to 0.727673,
     while LFM2.5 changed from about 0.358738 to 0.358434. The resulting Canary
     scores moved more visibly, from about 0.11809 to 0.11523 for Boldt and
     from about 0.26898 to 0.27536 for LFM2.5.
   - This is normal numerical nondeterminism in the CUDA execution path, not
     evidence that the corpus or configuration changed. A seed makes sampling,
     shuffling, and initialization reproducible, but it does not promise
     bit-for-bit equality for every parallel floating-point reduction and
     generated output.
   - Consequence: Canary remains only a crash, configuration, and aggregate
     gate. Model/configuration ranking uses the complete fixed Development
     coverage, and the later reproduction gate must compare immutable
     provenance plus bounded quality tolerances rather than demand identical
     adapter bytes or predictions. Prevention: never promote or reject a
     candidate from a single tiny Canary delta, and never call seed equality a
     sufficient reproduction proof.

64. **Post-selection evidence must be produced by reproduction, not required from its raw reference**
   - The first reference-comparison hardening accidentally required each raw
     PRAXIST Complete arm to already contain `score_provenance` and the summary
     to already contain a `reproduction_boundary`. Those fields are created by
     the reproduction operator itself only after the new Complete evaluation,
     so the real `raw Complete -> reproduction` entry point was impossible.
     The focused test hid the cycle by synthesizing both fields on its fake
     reference before calling the operator.
   - Independent review also found that the freezer checked score provenance
     but did not yet require the reproduction boundary, comparison, or matching
     successful one-shot ledger. A manually augmented summary could therefore
     bypass the intended exact-reproduction obligation. In addition, inherited
     PRAXIST run identity could let the primary evaluator reuse a same-run
     result instead of performing a fresh training pass.
   - Fix: accept only the unaugmented PRAXIST Complete as the canonical
     reference and bind it at selection time by its summary hash, both actual
     adapter-directory hashes, variant/configuration, prompt, trainer, primary
     evaluator, pinned base snapshots/revisions, and public row IDs/targets.
     Claim the fixed AppData one-shot ledger by that raw-reference hash before
     evaluator execution. Reserve a new empty output directory durably, strip
     PRAXIST/reuse environment identity, reject reuse markers or missing fresh
     adapters, and add score provenance, comparison, and reproduction boundary
     only to the newly produced output.
   - The freezer now accepts only that reproduced output when its boundary,
     comparison, current artifact hashes, raw selection binding, and matching
     successful terminal ledger all agree. Raw Complete and score-only
     augmented summaries fail closed. Independent validation passed 49/49
     reproduction/final-freeze tests, 10/10 export tests, and Python bytecode
     compilation without reading corpus JSONL or starting a GPU evaluation.
     Prevention: every one-shot workflow needs a fake end-to-end test that
     begins with the real upstream artifact shape; helper-generated downstream
     fields must never be preinstalled on the fixture that is supposed to test
     the producer of those fields.

65. **Critical-token F1 can improve while exact critical-value preservation gets worse**
   - W2 Development completed the fixed 600/60 coverage for both arms with
     correct active offset-weighted aggregates: 20,964 critical target tokens
     for Boldt and 20,695 for LFM2.5. The pair score was 0.67253, below the W1
     reference pair score of 0.69141, but pair score is deliberately not the
     arm selector.
   - Boldt W2 was unambiguously ineligible. Its critical-token F1 moved only
     from 0.53803 to 0.54065 and exact critical preservation stayed at 0/60,
     while arm score fell from 0.63707 to 0.59332, character similarity from
     0.92759 to 0.90047, unit F1 from 0.93615 to 0.90543, and structure from
     0.82452 to 0.59119. It breached every predeclared retention floor.
   - LFM2.5 W2 exposed the reason for keeping an exact-value gate. Character,
     structure, units, arm score, and critical-token F1 all met the retention
     floors; critical-token F1 improved from 0.68617 to 0.70755 and arm score
     from 0.74574 to 0.75175. Nevertheless, exact critical preservation fell
     from 5/60 to 4/60. W2 is therefore ineligible for LFM2.5 as well, exactly
     as the frozen rule requires.
   - Consequence: token-level overlap can reward many partially correct values
     even when one additional transcript contains a materially changed number,
     date, amount, unit, or citation. Prevention: never replace strict
     example-level preservation with an averaged token metric; require both
     retention floors and non-regressing exact critical examples before a
     weighted-loss candidate can reach Complete.

66. **One shared loss intervention can help one architecture and damage the other**
   - W4 Development completed the same 600/60 coverage with valid weight-4
     aggregates. Boldt degraded sharply: arm score 0.42455, character
     similarity 0.63957, critical F1 0.39346, structure 0.40266, and unit F1
     0.69139, with exact critical preservation still 0/60. W4 therefore fails
     every Boldt guardrail and W1 remains the final Boldt configuration.
   - LFM2.5 reacted differently. W4 retained 5/60 exact critical examples,
     improved critical F1 from W1's 0.68617 to 0.72512, and kept character
     similarity 0.98341, structure 0.98695, unit F1 0.98175, and arm score
     0.75844 above every fixed retention floor. It satisfies eligibility
     branch (b) and wins the LFM2.5 arm over both W1 and the exact-regressing
     W2 candidate.
   - The W4 pair score was only 0.59150 because Boldt's collapse outweighed
     LFM2.5's improvement. Selecting one pair-wide weight by that average would
     discard the useful LFM2.5 result; selecting W4 for both arms would ship a
     damaged Boldt model. The correct Complete candidate is mixed: Boldt W1
     and LFM2.5 W4, with every other hyperparameter and the seed unchanged.
   - Prevention: when two architectures share an experiment but respond
     differently, freeze eligibility and ranking per arm before the run. Treat
     pair score as reporting only unless the product truly requires one shared
     configuration across all arms.

67. **A one-shot reproduction must be task-wide and proven from the raw PRAXIST run**
   - A ledger keyed only by the reference file's byte hash is not a real
     one-shot boundary: reserializing the same JSON changes its hash and could
     create a second claim. The reproduction claim is now fixed per task and
     independent of reference formatting, while the exact selected reference
     remains separately hashed for provenance.
   - A plausible summary alone is insufficient evidence that PRAXIST actually
     produced the selected Complete result. The operator now requires a
     terminal successful PRAXIST run and cross-checks its scheduler job,
     generation, peer, mature work class, experiment ID, variant, result/log
     paths, committed generation snapshot, and task-project manifest. The
     separately maintained dataset metadata is bound by its own byte hash.
   - Fresh reproduction is proven from the captured primary-evaluator stdout:
     both arms must emit evaluation start, training progress, arm completion,
     and evaluation completion. Any known whole-result or per-arm cache/reuse
     event fails closed. The output directory is reserved empty before work so
     stale artifacts cannot be accepted as a new reproduction.
   - Freezing now requires the reproduced winning arm, not merely any arm with
     valid-looking metrics. The selected validation metrics are verified
     against that arm and included in the winner hash, preventing later metric
     substitution.
   - Root verification passed Python compilation and all 63 focused
     reproduction, finalization, and export tests without opening corpus JSONL
     or starting an evaluation. Remaining historical limitation: PRAXIST 0.5.0
     did not record the exact Hugging Face commit revisions in the raw run, so
     the reproduction binds the selected offline snapshots completely but
     cannot retrospectively prove their equality to an unrecorded raw-run
     revision. Prevention: future raw runs must record base snapshot revision
     and artifact hash before the first model load.

68. **A process-boundary change needs a real child-process test, not only mocked orchestration**
   - Independent review found that the first hardened operator still accepted
     nested cache/reuse markers, a non-Windows scheduler profile, an arbitrary
     experiment ID, interleaved arm events, implausible metric ranges, and
     redirected state paths. These checks now fail before either one-shot claim:
     cache markers are searched recursively, the mature job must use
     `windows_gpu` and exactly `<variant_id>-complete`, arm events must be
     sequential, normalized metrics stay in [0, 1], p95 cannot precede p50,
     and symlink/reparse redirection is rejected before path resolution.
   - Source hashes taken before and after work do not prove which already
     imported Python bytes executed. The operator now compiles the evaluator,
     trainer, and dashboard from immediately bound source bytes, disables stale
     bytecode use, and pins existing Windows input files against normal
     write/replace operations for the execution scope. The dashboard source is
     part of both the PRAXIST selection receipt and frozen winner manifest.
   - The exporter formerly accepted free arm/adapter/base paths and its isolated
     `python -I` child could not import the sibling finalizer. Export now accepts
     only the validated frozen-winner manifest; the child independently resolves
     the exact selected adapter/base, the manifest is checked again before
     publication, and the finalizer is loaded by exact path. A real isolated
     `--help` child smoke proves that this import boundary works.
   - A fresh runtime-loader refactor initially bypassed an old test mock and
     briefly started the real trainer child. Offline cache resolution stopped it
     at the first Boldt tokenizer lookup, before model weights, corpus access,
     training, or output. Fix: mock the loader seam that creates the fresh
     evaluator, not a stale module object. Agent and root verification then
     passed compilation plus all 77 focused tests; a second real isolated child
     smoke passed, and the productive reproduction ledger, final-test ledger,
     and canonical final output all remained absent.
   - Prevention: whenever production code adds fresh imports or subprocess
     isolation, keep one non-mocked launch/import smoke and separately assert
     that unit-test doubles patch the actual factory boundary. Full malicious
     administrator/kernel/process-injection resistance remains outside this
     user-process contract.

69. **A synthetic run fixture can invent a plausible path that real PRAXIST never creates**
   - The first reproduction validator expected a selected variant below
     `variants/gen_N/peer/variant_id/variant.json` because its unit fixture used
     that hierarchy. The actual PRAXIST run stores variants flat at
     `variants/<variant_id>/variant.json`; every other provenance check could
     pass while the real one-shot still failed before training.
   - A read-only preflight against the live run caught this mismatch before the
     task-wide claim existed. The reproduction receipt and final freezer now
     require the same real flat path and still bind generation, peer, mature
     scheduler job, experiment, result, log, configuration, and variant bytes
     through their independent authoritative records. No productive file was
     copied or synthesized to satisfy the validator.
   - The real-layout regression test failed on the former nested expectation
     and passed after the minimal correction. Compilation and all 77 focused
     tests passed again. Offline snapshot resolution also proved the default
     and explicit revisions resolve to the same cached Boldt and LFM2 bytes.
   - Prevention: before an irreversible claim, exercise every path assertion
     against one untouched real upstream run tree, not only a hand-built test
     tree. Treat a preflight that reports a missing terminal run summary or
     boundary as a correct wait; never manufacture the missing PRAXIST
     artifact or relax provenance to make the command runnable.

70. **Arm-wise critical weighting survives full coverage for LFM2.5 and materially raises exact preservation**
   - The mixed Complete trained both arms on all 1,600 fresh training pairs and
     evaluated the same 200 development-validation pairs. Protocol integrity
     and coverage passed. This remains selection evidence; the isolated
     200-case final split is still unopened.
   - Boldt W1 scored 0.78688 with character similarity 0.99125, critical-token
     F1 0.78926, exact critical preservation 19/200 (0.095), unit F1 0.98773,
     exact units 190/200, structure 0.99750, and full-output exact match 16/200.
     Its small movement from the earlier W1 Complete is consistent with the
     already documented CUDA variation and does not change its conclusion.
   - LFM2.5 W4 scored 0.93253 with character similarity 0.99873,
     critical-token F1 0.95548, exact critical preservation 132/200 (0.66),
     unit F1 0.99694, exact units 197/200, structure 0.99766, and full-output
     exact match 125/200. It peaked at 5.45 GB VRAM and remained inside the
     8-GB Windows GPU budget.
   - Relative to the prior LFM2.5 W1 Complete, exact critical preservation rose
     from 101/200 to 132/200 and critical-token F1 from about 0.932 to 0.955,
     while character, units, and structure stayed near their guardrails. The
     critical weighting therefore generalized from 600/60 Development to the
     full 1,600/200 coverage rather than merely exploiting the smaller split.
   - LFM2.5 is the unambiguous Complete winner: score 0.93253 versus Boldt
     0.78688, with pair score 0.85971. It still changes at least one critical
   value in 68/200 validation cases, so promotion requires the exact one-shot
   reproduction, frozen winner binding, and unopened-family final test; the
   validation win alone is not a product-install signal.

71. **A Windows reproduction must interpret PRAXIST's WSL path provenance and keep lane metrics distinct from the primary metric**
   - The terminal run was executed through the Windows GPU profile, but
     PRAXIST's authoritative `run_dir` fields were serialized as
     `/mnt/c/...`. The Windows reproducer treated these as native paths and
     therefore failed its read-only preflight with `PRAXIST run path is
     invalid`. The final-test harness already translated this representation;
     the reproduction path resolver now applies the same bounded
     `/mnt/<drive>/...` to `<DRIVE>:\...` mapping while preserving native
     Windows paths unchanged.
   - The selected frontier receipt also carries two deliberately different
     metrics. Its primary `metric_name` is `pair_score` at
     `0.859706700416792`, while its lane axis is
     `boldt_critical_example_exact_rate` at `0.095`. Hard-coding the lane
     metric to `pair_score` rejected valid PRAXIST output. The operator now
     requires `pair_score` as the primary metric and separately accepts only
     one of the five task-declared `fresh_word_pair` axes, with matching
     reference value, `maximize` direction, and lane identity.
   - Synthetic fixtures now use the real unequal primary/lane combination.
     Negative coverage rejects an unknown axis, wrong value, wrong direction,
     or wrong lane; both producer and finalizer test the Windows mount-path
     translation. Compilation and all 80 operator tests passed.
   - A read-only receipt build against the untouched terminal run then passed
     with schema `scriber-fresh-350m-run-selection-receipt/v1` and binding
     SHA-256 `c236dc38a603852e527ccd2139672bcad46a615099cc130418bd2b5174f09b57`.
     No reproduction/final ledger, output directory, GPU work, or final-test
     read was created by these diagnostics.
   - Prevention: use realistic upstream receipts in fixtures, test every
     cross-OS serialized path representation, and run an untouched real-tree
     preflight before any irreversible one-shot claim. Never equate a
     frontier lane's ranking axis with the task's canonical primary metric.

72. **Windows console encoding can abort weight loading even when the UTF-8 trainer child is correct**
   - The first task-wide reproduction claim started the canonical Complete
     evaluator and loaded the public 1,600/200 train/validation rows, but the
     outer Windows Python process exposed `cp1252` stdout. A Unicode progress
     glyph from model-weight loading therefore raised `UnicodeEncodeError`
     while the child output was copied to the live console.
   - The durable evidence proves the stop occurred before the first optimizer
     step: the transcript contains only `evaluation_start`, Boldt
     `arm_loading`, and weight loading at 0 %. There is no `train_progress`,
     `arm_complete`, `evaluation_complete`, validation, metric, adapter,
     result, summary, or LFM output. The failed output remains unchanged with
     a 339-byte transcript (SHA-256
     `142e2dc6f9a9f164d1628d78def045672b1cf397be1f8043917c7a69d4349a69`)
     plus one empty `boldt/` directory; the original attempt and failed
     terminal ledgers were not deleted or rewritten.
   - The stdout tee now writes the exact text to the UTF-8 durable transcript
     first and degrades only an unencodable live-console copy with replacement
     characters. An append-only, one-use recovery contract was implemented
     and tested without touching the productive ledger: it preserves the two
     original files, binds the failed footprint, derives one fixed sibling
     output, and would add separate recovery attempt/terminal receipts.
     Compilation and the complete 91-test operator suite passed, including a
     synthetic CP1252 failure, full two-arm continuation, and finalizer checks
     for all four ledger files.
   - The user then explicitly accepted the existing terminal PRAXIST evidence
     and ended all further Boldt work. Consequently the productive recovery
     was never claimed or run. Promotion now continues only with the already
     fully trained LFM2.5 W4 adapter from the successful PRAXIST Complete run;
     no additional Boldt GPU time is permitted.
   - Prevention: force or tolerate Unicode at every Windows stdout boundary,
     test progress-bar output against a legacy code page, and distinguish
     public-row access from the first actual optimizer step when classifying a
     failed training attempt.

## First complete protocol-v2 arm: Boldt

- The complete Boldt rank-16 arm trained on all 1,600 training pairs and was
  evaluated on all 200 development-validation pairs. This is complete
  validation evidence, not the untouched final-test result.
- Score 0.7833; character similarity 0.9904; critical-value F1 0.7843;
  exact critical-value preservation 17/200 (0.085); unit F1 0.9866; exact unit
  preservation 188/200 (0.94); structure 0.9975; full-output exact match
  15/200 (0.075).
- Median generation latency was 6.91 seconds and p95 was 9.01 seconds. The
  paired train/evaluation arm took 1,754 seconds, used 100 optimizer steps, and
  peaked at 2.50 GB VRAM. Mean training loss was 0.1943 and the last logged
  step loss was 0.0419.
- Compared with the smaller development stage, the full corpus materially
  improved structure and critical-value F1. The strict result is still unsafe:
  183/200 rows changed at least one critical value. Public review examples
  still include `193.495 € -> 173.417 €`, `184.140 € -> 164.404 €`,
  `21,80 €/m² -> 31,80 €/m²`, and `§ 129 -> § 139`.
- Consequence: Boldt remains a valid comparator but cannot be promoted on this
  configuration. LFM2.5 must complete the same full split before the next
  PRAXIST hypothesis is chosen.

## First complete protocol-v2 paired comparison

- Both arms completed all 1,600 training and 200 validation pairs under the
  fixed randomized split. Protocol integrity passed, coverage and effort are
  both 1.0, and the result is mature PRAXIST frontier evidence. It is still
  development validation, not the untouched final test.
- Pair score: 0.8424. LFM2.5 wins with 0.9014 versus Boldt at 0.7833.
- LFM2.5: character similarity 0.9991; critical-value F1 0.9315; exact
  critical-value preservation 101/200 (0.505); unit F1 0.9994; exact unit
  preservation 199/200 (0.995); structure 0.9998; full-output exact match
  101/200 (0.505).
- Boldt: character similarity 0.9904; critical-value F1 0.7843; exact
  critical-value preservation 17/200 (0.085); unit F1 0.9866; exact unit
  preservation 188/200 (0.94); structure 0.9975; full-output exact match
  15/200 (0.075).
- LFM2.5 median/p95 generation latency was 7.00/8.46 seconds, versus
  6.91/9.01 seconds for Boldt. LFM2.5 peaked at 5.32 GB VRAM and Boldt at
  2.50 GB; both fit the RTX 4070. The complete pair took 3,904 seconds.
- LFM2.5's mean training loss was 0.0719 and its last logged loss was 0.0090;
  Boldt's were 0.1943 and 0.0419. The lower loss aligns with LFM2.5's lead but
  does not make its remaining value changes acceptable.
- Consequence: LFM2.5 is the clear current leader, yet it still changes at
  least one critical value in 99/200 validation rows. No product export or
  final-test opening is justified yet. Later PRAXIST generations must improve
  the strict exact-value rate while retaining the already near-perfect
  structure and unit fidelity.

## First protocol-v2 development comparison

- Fresh randomized split, 600 training and 60 validation pairs per arm.
- Candidate: Boldt rank 16 and LFM2.5 rank 8; both use learning rate 0.0002,
  weight decay 0.01, one epoch, warmup 0.05, accumulation 16, alpha 16, and
  dropout 0.05.
- Boldt: v2 score 0.6371, character similarity 0.9276, critical-value F1
  0.5380, exact critical preservation 0/60, unit F1 0.9362, structure 0.8245,
  p50 latency 6.58 seconds.
- LFM2.5: v2 score 0.7457, character similarity 0.9854, critical-value F1
  0.6862, exact critical preservation 5/60, unit F1 0.9623, structure 0.9957,
  p50 latency 5.51 seconds.
- Pair score 0.6914. LFM2.5 is the clear leader, but 5/60 fully preserved
  critical-value sets is not production-ready. The next experiments should
  target fidelity through more optimization exposure or bounded adapter
  changes, not optimize character similarity alone.

## Final split audit

- Train/validation/test contain 1,600/200/200 unique examples and all 16
  canonical profiles plus all 11 canonical error tags.
- Validation is intentionally in-domain for iterative selection; its nearest
  training-target word-5-gram similarity is 0.710 on average.
- The one-time final test is a harder unseen-family robustness check: nearest
  training-target similarity is 0.279 on average, at most 0.514, and 0/200
  targets reach 0.7. It has no exact topic, canonical-family, raw-hash, or
  normalized source/target fingerprint overlap with train or validation.
- Because complete family isolation changes the profile mix, the final score
  must be described as robustness evidence rather than a calibrated estimate
  of production frequency.

## First technical CUDA smoke

- 96 training pairs, 12 validation pairs, one LoRA epoch-equivalent pass.
- Boldt: score 0.235, final logged loss 0.713, about 358 seconds including
  generation.
- LFM2.5: score 0.749, final logged loss 0.264, about 215 seconds including
  generation.
- LFM2.5 is the early quality leader, but these numbers used the rejected
  whole-topic split and are therefore not eligible for model selection.

## First valid development comparison

- Fresh randomized split, 600 training pairs and 60 validation pairs per arm.
- Boldt rank 16: score 0.8489, character similarity 0.9256, critical-token F1
  0.5970, structure 0.8155, p50 generation latency 6.70 s.
- LFM2.5 rank 8: score 0.9391, character similarity 0.9846, critical-token F1
  0.7520, structure 0.9951, p50 generation latency 5.40 s.
- Pair score 0.8940; LFM2.5 is the clear current leader.
- The aggregate score is too optimistic for production use because character
  similarity dominates while both models still change critical values. Review
  examples include `193.495 € -> 103.945 €`, `491 m² -> 424 m²`, and
  `217 Mbit/s -> 228 MB/s` for LFM2.5; Boldt changes even more values and also
  loses or duplicates some list items.
- Consequence: later PRAXIST selection must weight critical-token preservation
  more strongly and retain concrete veto/reporting for changed numbers, units,
  dates, times, and legal citations. A high character score alone is not a
  promotion signal.

## 2026-09-01: LFM-only restart from the new Word corpus

- The user ended all Boldt work and requested a complete LFM2.5-only restart
  from `LiquidAI/LFM2.5-350M-Base`. Boldt adapters, checkpoints, evaluations,
  and further GPU jobs are now out of scope.
- Useful LFM engineering is retained: the exact pinned base revision, native
  Windows BF16/CUDA runner, Word native-list extraction, deterministic
  family-aware randomization, completion-only LoRA loss, critical-token
  weighting, fidelity metrics, UTF-8 process handling, and iterative
  dashboard. Training state is not retained: no previous JSONL, adapter,
  checkpoint, optimizer state, prediction cache, result, or PRAXIST run is an
  input to the new task.
- The isolated task root is `fresh_windows_lfm2_350m`. Its sole authorized
  source is
  `C:\Users\Alexander.Immler\Downloads\2000_deutsche_Briefe_STT_Postprocessing_Word_native_Listen.docx`,
  size `1,551,320` bytes, SHA-256
  `cea3fc836a59a108164058530994de4f6f08342bf37dc39524ebdbaec1e3240c`.
  The extractor now refuses another path, size, hash, seed, table count, or an
  already existing output directory.
- The DOCX was freshly extracted once. All 2,000 labelled 4x1 Word tables
  passed structural validation. The randomized seed remains `3502026`; this
  preserves the known family boundary while independently shuffling every
  split and avoids moving formerly public validation families into a newly
  named holdout.
- Fresh split bindings are: train `1,600` rows, SHA-256
  `308082db85533bf266ae972c9c188777cf434bef10977a0e4980db73b8f102ab`;
  validation `200` rows, SHA-256
  `518140ce1c0a993bb302d7257fc4a7e30d7327aed6af599a0b864de4940066a8`;
  sealed final test `200` rows, write-time SHA-256
  `3e6853539dbd1d8dd43117b4862e400d4c58eff22a1896104d3b9aefb324e877`.
  The test bytes were not reopened after writing and remain unavailable to
  trainers, PRAXIST, dashboards, ordinary tests, and debugging.
- The public train and validation splits cover the same 87 canonical topic
  families and all eight source-number bands. Validation order begins with
  source indices `546, 1846, 759, 1838, 377`, directly confirming that it is
  not an ascending-number tail split. The final holdout contains eight
  canonical families with zero public-family overlap.
- The prior successful LFM W4 result remains hyperparameter guidance only. The
  new search therefore starts by retraining W4/rank 8/alpha 16/LR 0.0002 from
  the untouched base as a fresh baseline, then tests rank 16/alpha 32 and two
  epochs at LR 0.0001. W3 is reserved only if both primary hypotheses fail.
  Exactly one Development winner may receive one fresh Complete run.
- A PowerShell host may display UTF-8 German strings from the large stats JSON
  as replacement characters even though the JSON file is written as UTF-8.
  Prevention: machine checks use byte hashes and UTF-8 reads; human-facing
  dashboards are rendered from Python with explicit UTF-8 instead of trusting
  the host console code page.

### Windows/WSL execution boundary for the restart

- PRAXIST 0.5.0 is installed natively on Windows, but its own Codex-native
  doctor reports `win32; research runs require Linux, macOS, or WSL` and marks
  the platform unavailable. This is why the controller cannot be moved fully
  to Windows. WSL PRAXIST 0.5.0 passes the same doctor; all expensive work
  remains native Windows: dataset extraction, offline base loading, CUDA BF16
  LoRA training, validation generation, and HTML/PNG rendering.
- PRAXIST's installed task-manifest builder skips a top-level `data` directory
  before reading or hashing files. Therefore resolving/starting this task does
  not open the sealed final JSONL. The evaluator separately hashes only the
  public train and validation bytes and carries the final split's authoritative
  write-time metadata without opening its file.
- The first controller start was intentionally stopped before any variant or
  evaluator job existed after an independent audit found contract gaps. It
  produced no adapter, checkpoint, prediction, or GPU training. The definitive
  restart must resolve a new manifest after the fixes rather than resume that
  controller-only run.
- Prevention added before the definitive run: exact B0/H1/H2/H3 and Complete
  variant allowlist, exact seed and six design dimensions, full config objects,
  exact current-run scope via `PRAXIST_RUN_DIR`, atomically claimed result
  directories, current-attempt-only adapter paths, pinned base/prompt/dataset
  checks, bounded trainer termination, and explicit peer terminal signals.
- The first post-audit Canary was rejected before model loading because WSL
  environment variables such as `PRAXIST_PEER_ID` are not automatically
  transported into a native Windows executable. It therefore consumed no GPU
  training and created no adapter or checkpoint. Prevention: the WSL evaluator
  now validates peer and logical generation first, passes both as required CLI
  arguments to the Windows trainer, and the trainer independently checks the
  exact peer/stage allowlist and generation `0` before claiming output or
  loading the model. Arbitrary cross-boundary environment inheritance is no
  longer part of the protocol.

### QAD quantization gate for the later export

- Liquid publishes an official
  [`LFM2.5-350M-QAD-Q4_0.gguf`](https://huggingface.co/LiquidAI/LFM2.5-350M-GGUF/tree/main)
  and reports that QAD recovers 73.4% of the BF16-to-Q4_0 loss while retaining
  96.5% of BF16 average performance. This proves that QAD-Q4_0 is feasible for
  Liquid's post-trained 350M model, not that its finished GGUF can be applied
  to this separate Base fine-tune. See the
  [Liquid QAD report](https://www.liquid.ai/blog/qad).
- QAD is a second training phase, not a converter flag. The final BF16 model is
  frozen as teacher; an identical student experiences fake quantization in its
  forward pass and is optimized against the teacher distribution. The NVIDIA
  paper uses forward KL at temperature 1 and cautious learning rates around
  `1e-6` to `1e-5`. See
  [arXiv:2601.20088](https://arxiv.org/abs/2601.20088).
- No official recipe currently reproduces Liquid's GGUF Q4_0 fake-quantization
  and export for an arbitrary LFM2.5 fine-tune. NVIDIA ModelOpt implements QAD
  for NVFP4/FP8/INT4 and NVIDIA runtimes, not equivalent llama.cpp GGUF Q4_0;
  its format must never be mislabeled as Liquid QAD-Q4_0.
- On 2026-09-01 the user made QAD mandatory and explicitly prohibited parallel
  creation or comparison of other quantizations. Export policy is therefore
  now BF16 only as the frozen teacher plus exactly one genuine QAD-Q4_0 student;
  ordinary Q8_0, Q5_K_M, Q4_K_M, and PTQ-Q4_0 are not product candidates and
  must not be generated as fallback experiments.
- The implementation gate remains strict: the student forward pass must apply
  fake quantization equivalent to the exact physical llama.cpp Q4_0 tensor
  layout across LFM2.5's hybrid blocks, learn from the same frozen BF16 teacher
  by forward KL at temperature 1, and export to the standard Q4_0 GGUF runtime
  path. If equivalence cannot be proven, stop rather than silently substituting
  another quantization or mislabelling PTQ as QAD.
- Inspection of Liquid's official `LFM2.5-350M-QAD-Q4_0.gguf` establishes the
  exact product routing: 148 physical tensors, comprising 92 internal `Q4_0`
  matrices, one physical `Q6_K` tied token/output tensor, and 55 F32
  RMSNorm/ShortConv tensors. The logical `model.embed_tokens` and `lm_head`
  views must therefore share one original parameter even while both forward
  paths see the same Q6_K fake-quantized value.
- The b10158 oracle tests are byte-exact, not tolerance comparisons. Q4_0
  matches boundary cases plus 10,000 random CPU blocks; Q6_K matches boundary
  cases plus 500 random CPU blocks, whose random-block payload is 105,000
  bytes; the initial CUDA Q4_0 prototype matches 10,000 blocks; and the focused
  CUDA test matches 2,048 Q4_0 plus 256 Q6_K blocks. The first focused suite
  additionally checked Q4_0/Q6_K identity-STE gradients, fail-closed block
  sizing, exact 92/1/55 LFM routing, and native tied-weight restoration. It
  passed 7/7 in 1.624 seconds with `OK`. After adding an eighth
  BF16-source/cache regression, the expanded suite passed 8/8 in 1.502 seconds
  with `OK`.
- Q6_K initially disagreed because its levels had been treated as one generic
  global/C-order sequence. ggml instead works per 256-value block with sixteen
  16-value subgroups and emits `ql`/`qh` in a particular 128-half/32-chunk
  order. The fix preserves that subgroup layout and packing, reproduces
  `make_qx_quants` round-to-nearest-even behavior, strict candidate comparison,
  left-to-right accumulation, and FP16 super-scale storage. Prevention: never
  replace reference evaluation order or byte packing with an algebraically
  similar vectorization until exact b10158 oracle bytes prove equivalence.
- The public-source boundary is now explicit. Liquid proves the artifact,
  runtime format, and headline recovery but does not disclose optimizer,
  learning rate, data mix, schedule, fake-quant backward, or training code.
  NVIDIA/arXiv prove forward KL, temperature 1, a frozen teacher, causal shift,
  and label masking; NVIDIA's actual ModelOpt recipe is NVFP4 W4A4 with FP8 KV
  cache and is not llama.cpp Q4_0. Optimizer, fixed LR, accumulation, epoch
  count, STE, and tensor-routing implementation must be logged as local Scriber
  choices rather than attributed to Liquid.
- A corpus-free real-model instrumentation smoke loaded the pinned Base with
  148 tensors, resized the vocabulary from 65,536 to 64,400 while preserving
  its tie, and installed the exact 92 Q4_0 / one shared Q6_K / 55 F32 routing
  in 2.4556 seconds. CUDA peak after instrumentation was 3.043 GB. A four-token
  forward returned finite BF16 logits of shape `(1, 4, 64400)` in 0.780 seconds
  with a 3.057 GB peak, and removing the parametrizations restored the native
  tie. This establishes real-architecture instrumentation viability only; it
  is not a QAD training canary, convergence result, quality score, or GGUF
  runtime acceptance.

### First fresh LFM Canary evidence

- The new WSL-to-Windows identity transport passed in the live scheduler. The
  `fresh-w4-baseline` Canary loaded only
  `LiquidAI/LFM2.5-350M-Base@9960764e...`, trained 96 fresh examples for six
  optimizer steps on the RTX 4070, and validated 12 public cases. Peak VRAM was
  3.37 GB; no Boldt process or inherited model state was involved.
- Canary score was `0.37185` with character similarity `0.76558`, structure
  `0.39190`, critical-token F1 `0.13760`, critical-example exact rate `0`, and
  unit-example exact rate `0.25`. This is a successful functional gate, not a
  quality result: only 6% of the public training/validation protocol was used.
- Qualitative review exposed the important failure class hidden by character
  similarity: the small Canary often copied most prose while mutating numbers,
  dates, units, and legal citations; four visible samples also repeated until
  truncation, and some emitted forbidden labels such as `Bereinigte Fassung:`.
  Prevention: Development/selection must prioritize critical exactness,
  number-unit coupling, repetition/truncation, and forbidden-prefix failures;
  character similarity alone must never promote a model.
- A reuse audit confirmed that W4, rank 16/alpha 32, longer-gentler, the
  896-token ceiling, the 65,536-to-64,400 vocabulary resize, explicit embedding
  saving, fidelity-v2 semantics, UTF-8 process boundaries, and the WSL/Windows
  split are all present in the fresh task. GGUF merge ordering remains
  intentionally deferred until one adapter is frozen.
- One prior limitation remains: fixed seeds do not make the CUDA path bit
  identical. Enabling deterministic kernels after B0 would invalidate the
  active controlled comparison, so the live run is not modified mid-protocol.
  Selection review must treat a one-case Critical-Exact change or another very
  small delta as possible numerical variance; a close winner requires a
  consistently deterministic confirmation before product promotion. A clearly
  material Development improvement can proceed to the one frozen Complete run.

### Fresh W4 Development baseline

- The first canonical Development result trained 600 fresh rows and evaluated
  the same first 60 randomized public-validation rows used by every hypothesis.
  It completed 38 optimizer steps with final logged loss `0.09272`, peak VRAM
  4.60 GB, and no scheduler failure.
- Baseline metrics are score `0.76422`, character similarity `0.98407`,
  critical-token F1 `0.74023`, exact critical preservation `5/60`, unit-token
  F1 `0.97937`, exact unit preservation `54/60`, structure `0.98966`, and full
  output exact match `4/60`. This independently reproduces the earlier W4
  Development conclusion using only the newly extracted, cryptographically
  bound corpus.
- All ten stored public review samples still contain at least one material
  value/content error despite stable layout. Observed failures include amounts,
  identifiers, a date, `Mbit/s` becoming `MB/s`, and one surviving repetition.
  No obvious truncation remains at this stage. The next experiments therefore
  target literal fidelity while protecting the already strong structure.

### Bounded hypothesis Canaries

- Both fixed hypothesis Canaries passed the technical/integrity gate on the
  same 96/12 public subset. Rank 16/alpha 32 scored `0.48551`, critical F1
  `0.32491`, structure `0.59244`, and unit F1 `0.78373`. Longer-gentler scored
  `0.37528`, critical F1 `0.16416`, structure `0.34155`, and unit F1 `0.66151`.
  Both retained zero fully exact critical examples at this small stage.
- Rank 16 is the much stronger Canary routing signal, but Canary metrics remain
  partial and promotion-ineligible. Both candidates still proceed to the fixed
  600/60 Development comparison so the same randomized cases, not twelve
  examples or training loss, determine whether either intervention qualifies.

### Rank 16 Development result

- Rank 16/alpha 32 completed the controlled 600/60 comparison with score
  `0.80394`, critical F1 `0.82382`, exact critical preservation `8/60`, unit F1
  `0.99160`, exact units `57/60`, structure `0.99508`, and full exact match
  `6/60`. Against fresh W4 it improves score by `0.03972`, critical F1 by
  `0.08359`, and exact critical cases by three while improving every retention
  guardrail. This is materially larger than the previously observed CUDA
  micro-variation, so H1 qualifies.
- The additional capacity doubles trainable LoRA parameters from 2,998,272 to
  5,996,544 and lowers final logged loss from `0.09272` to `0.04910`. It also
  raises unquantized validation latency: p50/p95 `14.75/25.15` seconds versus
  W4's `10.36/15.64`. Quality selection and product latency must remain separate:
  the later merged/quantized Windows Scriber runtime needs a real latency check
  before this capacity increase can ship.

### Development selection

- Longer-gentler also qualifies: score `0.78775`, critical F1 `0.79023`, exact
  critical preservation `7/60`, unit F1 `0.98049`, structure `0.99070`, full
  exact match `5/60`, and p50/p95 `12.89/15.04` seconds. It improves over W4
  without doubling adapter parameters, but it remains below rank 16 on the
  predeclared primary ordering.
- Final Development order is rank 16 (`8/60`, critical F1 `0.82382`, score
  `0.80394`), longer-gentler (`7/60`, `0.79023`, `0.78775`), then W4 (`5/60`,
  `0.74023`, `0.76422`). Rank 16's one-case exact lead over longer-gentler is
  reinforced by a `0.03359` critical-F1 lead and `0.01618` score lead, while
  every retention guardrail improves. H1 is therefore selected for the one
  fresh 1,600/200 Complete run. H3 is correctly skipped because primary
  hypotheses qualified.

### Complete run and final-data policy

- PRAXIST launched exactly one `fresh-selected-complete` job from the untouched
  pinned LFM2.5 Base snapshot. Its effective configuration is the selected H1
  configuration (`learning_rate=2e-4`, one epoch, LoRA rank 16/alpha 32,
  critical-loss weight 4), with 1,600 training rows and all 200 public
  validation rows. The launch identity is generation 0, peer 0, stage
  `complete`; no earlier adapter or checkpoint is resumed.
- The 1,600/200/200 split intentionally separates three roles: weight fitting,
  configuration selection, and one independent final measurement. The final
  200 rows are not unused; opening or training on them before the winner is
  frozen would turn the reported quality into training-set evidence.
- After the H1 Complete artifact and every export choice are frozen, an
  explicitly separate final operator may evaluate that fixed artifact exactly
  once on the sealed 200-row test. Only after this evidence is recorded may a
  distinct production model be retrained with the already fixed configuration
  on all 2,000 rows. That all-data artifact maximizes data use but must be
  labelled as a production retrain without a remaining independent test; it
  must not inherit the frozen candidate's test result as if it were measured
  on the same weights.
- The unattended Windows host had no active execution-state guard even though
  Complete evaluation and finalization span hours. The existing bounded
  `keep_training_awake.ps1` helper was therefore started hidden for eight
  hours; it sets only the system-required execution state and restores normal
  power behavior in `finally`.
- The one H1 Complete job finished successfully with all 1,600 training and
  200 public validation pairs, 100 optimizer steps, and no retry or scheduler
  failure. Score is `0.9715275`, character similarity `0.9995797`, critical
  token F1 `0.9811532`, critical-example exactness `171/200`, unit token and
  example exactness `1.0`, structure `1.0`, and whole-output exactness
  `167/200`. Peak VRAM was 4.535 GB; generation p50/p95 was 22.24/44.53
  seconds. Nine of ten stored reviews exactly matched their targets; the sole
  material failure changed `491 m²` to `401 m²`. This residual numeric failure
  confirms that the installed safety gate must remain active even at the much
  improved aggregate score.

### PRAXIST STOP_SIGNAL closure

- After all seven scheduled jobs were terminal, both peers had published a
  semantic `STOP_SIGNAL` finding but the physical `gen_0/STOP_SIGNAL` sentinel
  was absent. PRAXIST therefore opened redundant continuation sessions even
  though no additional evaluator work was required. This was a control-plane
  signalling defect, not a model-training, scheduler-job, or Complete-result
  failure.
- The repair was deliberately bounded. It first verified the authoritative
  state: zero queued jobs, zero running jobs, seven completed jobs, zero
  failures, and one mature Complete result. It then closed admission through
  `freeze_generation(0, "complete_evidence_published")` and wrote the real
  filesystem sentinel atomically with
  `SynthesisTrigger._write_signal_atomic`.
- The resulting sentinel records `trigger_reason=complete_evidence_published`,
  mature quorum 1/1, `active_protected_pids=0`, and
  `active_generation_work=0`. PRAXIST then wrote `CLOSING_SIGNAL`, committed
  Generation 0, and finalized the run with `status: succeeded`, exit code 0,
  and exit condition `max_generations`. Prevention: a peer finding and the
  controller sentinel are separate obligations; after terminal evidence,
  verify both before allowing or diagnosing continuation sessions.

### Product-path parity gate

- The pinned llama.cpp b10158 converter/runtime is structurally LFM2-aware,
  but a LoRA/PRAXIST result is not yet a product artifact: the selected model
  still requires a safe BF16 merge, GGUF conversion, and real local-polishing
  runtime smoke.
- Training and score-time inference use the exact reviewed raw completion
  prompt with `${output}`, BOS, target plus EOS, and at most 384 generated
  tokens. The current Scriber manager historically uses `${transcript}`,
  inserts KEEP markers for protected values, and may request up to 4,096 new
  tokens. That mismatch makes PRAXIST quality evidence insufficient for product
  acceptance. Prevention: the LFM catalog/manager integration must introduce
  an explicit raw-completion contract matching training, then compare direct
  and manager outputs on public non-test cases; the safety fallback must remain
  enabled and any output mutation/repetition must fail closed to the raw text.

### Frozen-test, all-data retrain, and export operator

- A dedicated LFM-only operator now separates the three irreversible phases:
  freezing the terminal PRAXIST Complete artifact without opening the sealed
  split, evaluating that immutable artifact exactly once, and only then
  retraining a fresh production adapter on all 2,000 Word pairs. The production
  retrain reconstructs all splits directly from the bound DOCX, verifies their
  exact serialized hashes, records the seeded shuffled sampler order, and never
  resumes an adapter, checkpoint, or optimizer state.
- The all-data receipt is deliberately quality-neutral: it records 2,000
  trained pairs and exact lineage but contains no score, winner, promotion, or
  inherited test metric. This prevents the one-shot 1,600-row candidate result
  from being misrepresented as a measurement of the later 2,000-row weights.
- The initial export design could safely merge an exact production receipt into
  a tied BF16/no-LoRA LFM2.5 working model, but it also encoded a conventional
  GGUF matrix. The user's later QAD-only decision retires every conventional
  output before any real export occurred. Retain only the verified tokenizer
  -> Base resize -> adapter -> tied-embedding BF16 merge as the teacher/working
  stage. BF16 is not a product candidate; the only authorized product output is
  the later trained QAD-Q4_0 GGUF.
- `production_plan.json` is now schema v2 and authorizes exactly one deployable
  artifact: `QAD-Q4_0`. The older `export_lfm2_gguf.py` and
  `evaluate_exported_lfm2_gguf_public.py` entry points remain retired and must
  not be executed; no parallel quantization or diagnostic matrix is allowed.
- The original operator fixture suite passed 25/25 tests on Windows without
  opening `data/test.jsonl`, running a real merge, training a model, or
  quantizing weights. The first focused QAD quantization suite passed 7/7 in
  1.624 seconds and added physical b10158 Q4_0/Q6_K oracle parity, identity-STE
  behavior,
  fail-closed block sizing, and exact LFM tensor routing/tie restoration. These
  are numerical implementation gates, not evidence that QAD training or final
  GGUF export has already completed. The subsequently added eighth BF16
  cache-projection regression also passed; the expanded suite is 8/8 in 1.502
  seconds.
- Operator scripts initially treated an exploratory `--help` argument as a
  request to execute because three entry points had no argument parser. Their
  existing preconditions stopped before a test read, attempt claim, training,
  or artifact publication, and a filesystem/process audit confirmed no output
  or live operator remained. Fix: all four entry points now parse arguments
  before work, provide an explicit help path, and reject unknown arguments.
  The help probes and the full 25-test suite then passed again.

### Scriber generation-contract preparation

- Scriber now has a backward-compatible catalog schema 3 for raw-completion
  models. It requires an integer `generation_max_new_tokens` from 1 through
  4,096 for every variant, binds that value into the immutable catalog
  identity, and makes the manager use exactly that cap. The future LFM catalog
  can therefore reproduce the training/evaluation limit of 384 instead of the
  historical dynamic budget up to 4,096.
- Existing schema-1 Gemma artifacts retain their byte-for-byte catalog
  identities and dynamic generation behavior. Schema 2 may not silently carry
  an unbound cap; a plain-completion descriptor without an exact cap fails
  closed to the original transcript before invoking the runtime.
- Independent verification passed 143 focused local-polishing and packaging
  tests plus Ruff. This closes the generation-length mismatch only. BOS and EOS
  parity still require the real GGUF runtime, and the direct-versus-KEEP-marker
  comparison remains mandatory because LFM was trained on raw transcripts.
- The broader local-polishing Web API suite was initially blocked before test
  collection by 25 committed Python-2-style multi-exception clauses in
  `src/web_api.py` (`except A, B:`). A read-only audit proved that the file
  matched `HEAD` and that an in-memory parenthesized rewrite parsed cleanly.
  The applied fix changed only those clauses to `except (A, B):`; full parsing,
  CPython 3.14 byte compilation, and all 18 local-polishing Web API tests then
  passed. Prevention: parse the whole module after the first syntax failure,
  because fixing only the first line would have hidden 24 identical faults.

### Real QAD-Q4_0 GPU canary

> Superseded evidence: the numerical/memory observations below remain useful,
> but this canary is not an accepted QAD result. A later audit found an
> unauthorized global gradient clip in addition to Adafactor's internal
> `clip_threshold`. Its create-once output was preserved under
> `qad_development_canary_1step_invalid_external_clip_2026-09-01`; a corrected
> canary must replace it before Development training.

- The first real 16-example/one-step canary completed its QAD optimizer step
  but failed during post-training cleanup with `FrozenInstanceError`: code
  attempted to delete `teacher` from the deliberately frozen
  `LoadedQadModels` dataclass. No output directory had been published. The
  bounded fix retained the tokenizer locally and released the complete frozen
  container with `del loaded`; Python compilation and all seven trainer tests
  passed before retry. Prevention: immutable evidence containers must be
  released as whole objects, never mutated for memory cleanup.
- The retried canary succeeded on the exact 16 longest tokenized public
  training rows, including the longest 837-token case, and consumed 5,319
  completion tokens in one complete gradient-accumulation window. Forward KL
  was finite at `0.0173382`; all 148 parameter tensors and both the FP32 master
  and BF16 deployment projection changed. Exact routing remained 92 Q4_0, one
  tied Q6_K, and 55 F32 tensors.
- Peak CUDA allocation was 5.239 GB and peak reservation was 5.359 GB on the
  8 GB RTX 4070 Laptop GPU. This demonstrated memory feasibility only; because
  the optimizer algorithm was later invalidated, it does not clear the final
  math gate and is not a product GGUF or quality result.
- Before the first full Development attempt could waste the complete run, a
  static follow-up found the same immutable-container cleanup mistake in the
  Development backend after its validation phase. The active attempt was
  interrupted after roughly 50 seconds, before any model or receipt was
  published; its one empty, uniquely named staging directory was verified
  inside the operator root and removed. The backend now also retains the
  tokenizer locally and releases the whole `LoadedQadModels` value. A dedicated
  regression executes that backend with a real frozen bundle and mocked ML
  work; the focused QAD suite is now 19/19 green. Prevention: every wrapper of
  an already-tested lifecycle needs its own end-to-end cleanup regression,
  especially when the failure point occurs only after expensive work.
- The next audit found that the trainer applied global
  `clip_grad_norm_(..., 1.0)` immediately before `optimizer.step()`, even
  though the frozen recipe specifies only Adafactor's different, internal RMS
  `clip_threshold=1.0`. The first full run was stopped and discarded rather
  than falsely attesting that it followed the recipe. Fix direction: construct
  Adafactor with explicit `eps`, `clip_threshold`, `decay_rate`, and `beta1`;
  observe gradient finiteness/norm without mutation; persist the actual values;
  and reject any receipt whose runtime telemetry differs from the plan.
- After these fixes and 26/26 focused regressions, the corrected real canary
  completed on the same 16 longest public rows. It records
  `external_gradient_clipping=false`, the exact Adafactor parameters, and an
  observed (not clipped) gradient norm of `6.5870471`. Mean forward KL remained
  `0.0173382`; routing stayed 92 Q4_0 / one Q6_K / 55 F32, all 148 parameter
  tensors changed, and peak CUDA reservation remained 5.359 GB. This corrected
  output at `qad_development_canary_1step` is the accepted memory/math gate;
  the separately named external-clip output remains invalidated history.
- Live monitoring of the first corrected full attempt then exposed a reference-
  lifetime bug that a one-step canary could not show: the local list passed to
  `get_total_norm()` retained the just-cleared 1.4 GB gradient set into the next
  microstep, so process memory rose to roughly 7.4 GB with only about 526 MB
  free. The attempt was stopped before OOM. The norm and list are now deleted
  immediately after their scalar value is copied; optimizer ownership remains
  exclusively in each parameter's `.grad` field.
- A new 32-example/two-step longest-case canary proves the inter-step fix. Both
  windows completed with observed gradient norms `5.2986` and `6.7320`, mean
  forward KL `0.0155827`, all 148 tensors changed, and peak CUDA allocation /
  reservation of 5.240 / 5.555 GB. Prevention: memory canaries for accumulated
  full-weight training must cross at least one optimizer boundary and execute
  another backward pass; a terminal one-step sample cannot reveal stale local
  gradient references.

### Completed Development QAD training and canonical export

- The corrected Development run completed exactly one epoch over all 1,600
  training rows: 1,600 microsteps, 100 Adafactor optimizer steps, and no
  external gradient clipping. Mean training forward KL was
  `0.0024413964100313025`; the separate 200-row public validation produced a
  mean completion-token KL of `0.001193535659129908` over 52,915 tokens. All
  148 tensors changed, and peak CUDA allocation / reservation was 5.242 /
  5.594 GB. The create-once receipt and six-file model publication reopened
  successfully, while the sealed 200-row test split remained unopened.
- The first real GGUF export stopped before publication because the pinned
  b10158 `llama-quantize.exe` returns exit code 1 even for its valid `--help`
  usage output and exposes no conventional `--version` command. The exporter
  now treats return codes 0 and 1 as the only accepted capability-probe
  outcomes, requires the expected Q4_0 usage text, and independently binds the
  colocated `llama-server` version plus the exact quantizer file hash. Fixture
  runners reproduce the real exit-code behavior. Prevention: capability probes
  must validate both documented output and the tool's observed exit convention
  instead of assuming every help invocation returns zero.
- The next conversion reached the tokenizer and failed because the isolated
  converter environment used Transformers 4.57.6, whereas the saved LFM model
  carries the Transformers 5.16.1 `TokenizersBackend` metadata contract. The
  model and weights were left untouched. Updating only the isolated converter
  environment to Transformers 5.16.1 / tokenizers 0.23.1 restored local
  tokenizer loading and preserved the pinned b10158 converter/runtime.
  Prevention: probe the saved tokenizer locally with the converter Python
  before starting a multi-hundred-megabyte conversion.
- The successful canonical export published exactly two files: the sealed
  manifest and one 218,328,640-byte `Scriber-LFM2.5-350M-QAD-Q4_0.gguf` with
  SHA-256 `378711d917b029d49f27b6d7796fa6cc83a0d627156607b5eb23bd55c3e8f9a1`.
  Inspection observed the exact 92 Q4_0, one tied Q6_K, and 55 F32 tensor
  contract. The transient BF16 GGUF was deleted, PTQ remains forbidden, and no
  alternate quantization product was authorized or produced.
- The first real b10158 Windows runtime smoke exited before any evaluation data
  was processed. Loader diagnostics identified the exact cause: on this AMD
  iGPU plus NVIDIA dGPU laptop, b10158 requested
  `VK_KHR_shader_bfloat16`, while the selected Windows Vulkan ICD rejected that
  advertised experimental extension during `vkCreateDevice`. This was neither
  a GGUF nor a QAD-weight failure: CPU-only loading succeeded immediately.
- The bounded GPU fix selects `Vulkan1` (the RTX 4070), sets llama.cpp fit to
  `off`, and launches the child with `GGML_VK_DISABLE_BFLOAT16=1`. QAD-Q4_0
  does not need the disabled BF16 Vulkan path. The exact frozen GGUF then loaded
  on the dGPU; real tokenization added exactly one BOS token with ID 1, and the
  bound post-processing-prompt warmup returned four deterministic tokens in
  37 ms after startup. Prevention: on Windows hybrid-GPU hosts, bind the target
  llama.cpp device explicitly and run a real prompt/BOS smoke before opening an
  irreversible evaluation ledger; `--list-devices` alone does not prove that
  device creation will accept every extension detected by the runtime.
- The create-once QAD-only runtime evaluation then consumed all 200 public
  validation rows with real b10158 greedy generation. It achieved score
  `0.9665603723`, character similarity `0.9995519095`, critical-token F1
  `0.9777787921`, structure `1.0`, and exact match `164/200`; generation p50 /
  p95 was 653 / 887 ms. No selection, ranking, PTQ, alternate quantization, or
  sealed-test access occurred. This clears the Windows runtime gate for the
  exactly-once final test.
- Aggregate similarity still hides the model's main residual risk: 34 of the
  36 non-exact public outputs changed the extracted number sequence, including
  examples such as `364` -> `640`, `180.976` -> `180.876`, and `43.040` ->
  `43.40`. QAD retained nearly all Development quality but did not eliminate
  the small model's numeric substitutions. Prevention: Scriber must keep its
  conservative protected-value safety gate and return the raw transcript on
  any number/date/time/unit mutation; the local model must never be integrated
  as an unchecked text replacement.
- The sealed 200-row final split was then opened exactly once under host-local
  attempt `8af0cd99-b2c6-49bb-aa1f-e41cf22226fa`. The immutable QAD-Q4_0 GGUF
  completed with score `0.9552595151`, character similarity `0.9993833251`,
  critical-token F1 `0.9691271930`, structure `1.0`, and exact match `148/200`;
  runtime p50 / p95 was 579 / 672 ms. The durable receipt says
  `completed_exactly_once`, and the result is report-only with no retry,
  ranking, or selection. These metrics describe only the 1,600-row Development
  weights; they must never be copied onto the later fresh All-2000 production
  weights.

### Fresh All-2000 production SFT and QAD-only artifact

- The first production retraining launch stopped before loading weights or
  publishing output because its preflight compared the frozen model metadata
  against the abbreviated `{repo_id, revision}` binding used by an older test
  fixture. The frozen bundle intentionally contains a richer, exact model
  identity. The production launcher now validates the complete frozen binding,
  and its fixture mirrors that contract. All 25 focused tests plus four binding
  subtests passed before the retry. Prevention: preflight fixtures must use the
  exact frozen identity object rather than a convenient subset that can reject
  valid enriched metadata.
- The clean production SFT then trained once over all 2,000 fresh pairs in one
  deterministic shuffled epoch: 2,000 microsteps, 125 optimizer steps, seed
  `17029`, and sampler-order SHA-256
  `65ea17087a5ac1c6a9f4bc1a7295a47bb022b6a223ed1050e8a178724585f1ba`.
  It did not resume from Development, access a quality split, calculate a
  selection metric, or reuse any historical Scriber corpus. The output is a
  production training input, not new generalization evidence.
- Production QAD consumed those exact 2,000 shuffled rows for 2,000 microsteps
  and 125 Adafactor optimizer steps. Mean forward KL was
  `0.001792856738298724` across 530,645 valid completion tokens. All 148 tensors
  and 72,349,772 BF16 deployment-projection elements changed; the exact routing
  remained 92 Q4_0, one tied Q6_K, and 55 F32 tensors. The receipt records no
  external gradient clipping, no validation or selection, no PTQ, and no other
  quantization path. Its immutable receipt ID is
  `sha256:224f73655be64892185264d9e686a8bfe897de7e238359751ab38f25206d5839`.
- The canonical production export published only the manifest and one
  218,328,640-byte GGUF named
  `Scriber-LFM2.5-350M-Production-QAD-Q4_0.gguf`, with SHA-256
  `197d207e1d87cdb599b53bb6f0848b8d5123328718ab6d039349ead7978f0eac`.
  GGUF inspection reconfirmed all 148 tensor types and parity with the QAD
  routing receipt. The export manifest ID is
  `sha256:0dea4c88c67ab834f7fdedf6689f8de1bbcb58a22f39455a808522603998c128`.
  The transient BF16 conversion was not published, and no conventional PTQ or
  alternative quantized artifact exists.
- A UTF-8 runtime replay of the first ten bound corpus rows produced 10/10 exact
  outputs, 10/10 exact critical-token sequences, EOS in every case, and no
  repetition marker; median generation latency was 478 ms. Because all 2,000
  rows are production training data, this proves only that the exported model,
  tokenizer, prompt, and Windows llama.cpp path are internally coherent. It is
  not a held-out quality result. Two earlier tiny PowerShell-encoded ad-hoc
  prompts repeated to the token cap; malformed/very-short out-of-distribution
  input therefore remains a concrete reason to keep repetition detection,
  protected-value checks, and raw-transcript fallback enabled in Scriber.

### Product-integration and publication learnings

- The historical public protection policy cannot be copied into the LFM
  release. It identifies `gemma_lexical_v1` and was created for Gemma's
  tokenizer/vocabulary. Treating that file as model-neutral would make the
  catalog cryptographically precise but semantically false. The LFM product
  policy therefore uses a new closed schema and policy ID and binds the exact
  base revision, production tokenizer hash and 65,536-word vocabulary, both
  prompt contracts, `plain_text_v1`, the 384-token cap, and the explicit
  `raw_transcript_no_keep_markers` runtime-input rule. Legacy policy parsing is
  retained only for exact removal of old installations.
- The trained prompt file is 310 UTF-8 bytes and contains `${output}`; its
  SHA-256 is
  `372f879803334a68e310fe2e658c11678600baf0f4ef72834e4acd409f747dd6`.
  Scriber's catalog needs its own renderable placeholder `${transcript}`. The
  otherwise byte-identical 314-byte template has SHA-256
  `e0ff2d5297f3d4d5ae7b8af85ea1cf52a24704bfb2e61990eab6de52b42058d8`.
  Both hashes must be bound: the first proves training-source lineage; the
  second proves the exact runtime template. Prevention: never reuse a training
  placeholder literally in product code or silently hash only one side of the
  substitution boundary.
- Earlier plain-completion integration still inserted KEEP markers before
  generation and restored them afterward. The LFM model was trained and all
  reported evaluations were run on the raw transcript, so this was an input-
  distribution mismatch despite being a conservative safety technique. The
  QAD variant must render the raw transcript into the exact prompt and apply
  structural, content, repetition, number/unit, EOS, and token-budget checks to
  the candidate afterward. Any failed check returns the original transcript.
- The pinned Liquid base uses LFM Open License v1.0, not Apache-2.0. Its exact
  10,574-byte license has SHA-256
  `4d28ca14dedc0b3d0fcc2b3339f0e79931faa33874f3d24f522183a8fc70068c`;
  the pinned base tree contains no NOTICE. A public derivative must include the
  full license, retain applicable attribution, and prominently identify the
  modifications. Commercial use without a separate Liquid license is limited
  to legal entities below USD 10 million in annual revenue. Prevention: do not
  label the model card Apache, and publish LICENSE plus a clear modifications
  notice beside the single QAD artifact.
- The production export manifest itself is not suitable for public upload: its
  evidentiary bindings intentionally contain absolute local Windows and sealed-
  evaluation paths. Publication must instead use a sanitized portable manifest
  containing only public relative paths, exact hashes/sizes, aggregate evidence,
  and the single authorized QAD tensor layout. Training rows, predictions,
  adapters, Safetensors, transient BF16, executable tools, and alternative
  quantizations stay private and unpublished.
- Hugging Face's server-side model-card validator does not accept `lfm1.0` as
  the value of its closed `license` field, even though the upstream model page
  displays that name. The first publication commit was rejected before any
  user file was added. The valid custom-license metadata is `license: other`,
  `license_name: lfm1.0`, and `license_link: LICENSE`; the README and portable
  manifest hashes were recomputed before retry. Prevention: run the Hub YAML
  validator contract before a large atomic commit, especially for custom model
  licenses.
- The successful public QAD-only commit is
  `083194c5a2efe1b611133d363ebc97391b8bff05` in
  `Buttermilk03/scriber-lfm2.5-350m-polishing-de-qad-v1`. Its first anonymous
  full-file verification was interrupted by a CDN connection reset, and an
  initial range retry selected a broken direct IPv6 route. The final verifier
  forced IPv4, sent no authorization header, requested deterministic byte
  ranges, reconstructed every file in order in memory, and matched all six
  public sizes and SHA-256 values, including the complete 218,328,640-byte
  GGUF. Prevention: verify large anonymous artifacts with restartable range
  reads and explicit network-family telemetry instead of treating a transient
  full-stream reset as a model or publication failure.
- A later product-runtime audit found that publication had missed the separate
  PRAXIST Generated Output terms. The pinned PRAXIST Fair Source License calls
  weight updates and weight files Generated Output, requires the exact product
  attribution `Praxist by Sapient Intelligence` when output is made available
  to third parties, and permits the free license only while aggregate licensee
  and affiliate revenue remains below USD 1 million. The model repository was
  therefore changed from public to private without deleting its commit or
  files; an anonymous API request then returned HTTP 401. Prevention: treat the
  base-model license and the training-tool output license as independent
  release gates, bind both before the first upload, and never infer an owner's
  revenue status.
- The first real product-manager E2E downloaded all five pinned artifacts
  anonymously (218,347,884 bytes total), verified every size and SHA-256,
  prewarmed the frozen Windows runtime in `vulkan_compat`, and left no
  `llama-server` process behind. Functional acceptance was nevertheless 0/5 on
  fresh cases, so the canonical Scriber model root remained untouched. Three
  short dictations generated a plausible first pass and then repeated it plus
  `Bereinigte Fassung:` until the 384-token cap; an explicit marker stop ended
  the loop but exposed a wrong `2024` -> `2026` date mutation. Two longer fresh
  dictations reached EOS but were rejected for name/number changes, including
  one real signature-name deletion. The Development QAD weight showed the same
  short-input loop, proving it predates the All-2000 production stage.
- The same E2E exposed that the current safety contract is not aligned with the
  task contract. It rejects desired transformations such as spoken legal
  references to `§ ...` and `erstens`/`zweitens` to list numerals, while its
  fuzzy lexical threshold can accept unrelated one-token meaning or proper-name
  substitutions. Prevention: do not relax the fail-closed fallback merely to
  raise acceptance. First close false-accept paths, then model spoken-number,
  list, legal-reference, capitalization, and signature equivalence explicitly,
  and require fresh end-to-end cases before installing or enabling the model.
- Current promotion status is therefore `withheld`: QAD-Q4_0 remains the sole
  candidate, no alternative quantization or comparison is permitted, the Hub
  repository remains private, and the canonical Scriber model root remains
  untouched. Technical download/hash/runtime success does not override the 0/5
  functional result. At that point the owner confirmation for PRAXIST licensing
  was still open.
- The first corrected private review head was
  `aabcdb5edbc6d243871e0378c6cda9e28a8d44b0`. Authenticated verification matched
  the 5,684-byte publication manifest at SHA-256
  `4a7a48e0d1ffae7e6c11bc4f37c9ea0b1f74c8df0b6d934e196c2f5c3d17603e` and the
  1,809-byte `MODIFICATIONS.md` at SHA-256
  `aa723a9147c58436a37c1068bd723e636f1dba88b0697bd13f90a8ab584efa51`;
  anonymous access returns HTTP 401. The shipping catalog now has
  `revision=None`, so the UI and direct API both fail closed without attempting
  a private or credentialed download. Prevention: after changing publication
  metadata, update the candidate byte contract and remove every stale public
  revision claim before a build can consume it.
- On 2026-09-01 the owner explicitly confirmed aggregate annual revenue of USD 0
  including affiliates and that the work is open source. This passes the
  PRAXIST free-license revenue gate; it does not override the failed 0/5
  product-runtime gate. The private documentation-only head is now
  `d6bd4faa8347811423f704f181a4bfb3723df40f`. Authenticated byte verification
  matched the 6,195-byte manifest at SHA-256
  `7878a540dc116de53006593265825c990dd07f1437cbd41a74601b17b5a124b8`, the
  2,204-byte `MODIFICATIONS.md` at SHA-256
  `6176dc0d5500ab25221f7a0877f51d8fb36a22336bd575b0a2bd29db3f3278f1`, and the
  6,103-byte README at SHA-256
  `1d9c7c6262d24fbf07f34f683b28fc667738a9fc1d2e3af2bf16ff0fc77266de`;
  anonymous access remains HTTP 401. Release, publication, installation, and
  activation remain blocked until a fresh runtime E2E passes.

### Safety hardening before short-input recovery

- The public TDD seam is `LocalPolishing.polish(...)`, not a private validator.
  Every regression therefore installs a synthetic QAD-only catalog, exercises
  the same manager path as the product, and observes accepted text or the exact
  original fallback. The focused backend set now passes 400/400 tests; the
  local-polishing file itself passes 160/160, and targeted Ruff is clean.
- Global fuzzy word equivalence was unsafe for one-token meaning and proper-name
  changes. It is now exact by default, with only the reviewed typo pair
  `Mietvertag`/`Mietvertrag` admitted. Alexander/Alexandra, Müller/Möller,
  Schmidt/Schmitt, and `Frau Berger`/`Frau Bergner` all return the original.
- Spoken punctuation, line/paragraph breaks, legal references, compact dates,
  amounts, and units need positive semantic bindings. Merely deleting command
  words is insufficient: the validator now requires the corresponding mark or
  break at its bound context and rejects ambiguous noun uses such as
  `das Komma` or legal `Absatz 3`. Polarity inflections share one lemma, while
  polarity removal and changed legal section numbers still fail closed.
- A first `_FormatCommandAnchor` edit accidentally displaced `_NumberMention`'s
  default fields and broke test collection. Restoring the fields to their
  owning dataclass fixed the issue. Prevention: after inserting adjacent frozen
  dataclasses, run collection immediately before testing behavior.
- The failed product cases reveal a distribution gap, not a QAD serialization
  fault: every bound source letter has at least 760 characters, while three
  short dictations looped after an initially plausible answer. The recovery
  dataset therefore derives short identity/EOS and conservative noisy examples
  only from the new targets, keeps train and validation parent IDs separate,
  and withholds the five product-E2E cases from training.
- `praxist_task/AGENTS.md` now points directly at the active
  `fresh_windows_lfm2_350m_short_recovery` branch. It permits only hash-bound
  train/validation inputs for Development, retains a separate fresh all-data
  Production retrain, and keeps QAD-Q4_0 plus the fresh Windows E2E as explicit
  completion criteria.
- The earlier H1 Complete adapter is a better recovery parent than another
  untouched-base SFT: it already learned the 1,600 long training parents and is
  fully bound by directory SHA-256 `03ed1273f960aa0aa03d43e6a8d6d16d273bc3cac14ad551ece9fa8057ff3100`.
  Recovery therefore loads that exact rank-16/alpha-32 PEFT adapter trainably,
  verifies all recorded files and 5,996,544 trainable parameters, then uses a
  fresh optimizer only on the 3,200 new short children. The 200 original plus
  400 short validation cases are regression checks; they are not independent
  final evidence because the original validation split informed H1 selection.
- Production follows the same fixed recipe from the exact prior all-2,000
  adapter (`b04e5f22630f08c4dae2f2aa0d20aa51698310e57688463d680ef0756c0dbda2`)
  and 4,000 derived short children. QAD must then start from fresh student
  weights against the newly merged teacher and see all 2,000 original plus
  4,000 short contexts. This reuses learned SFT capability without inheriting
  stale optimizer state, QAD weights, or Development metrics.
- EOS coverage alone does not address the observed date mutation and signature
  name loss. The short builder must prefer unchanged target substrings carrying
  dates, numbers, units, legal references, names, or signature cues whenever a
  suitable 20-350-character span exists, and record category coverage in its
  manifest. The five already-observed product-E2E cases remain regression gates
  and never become training examples. Because their failure modes influenced
  this recovery design, the independent final product gate must additionally
  use new cases created only after the recipe is frozen.
- The first create-once protected augmentation was bound at
  `fresh_windows_lfm2_350m_short_recovery/data/short_aug_v2`: 3,200 training
  rows (SHA-256 `70fb71541c09c287fa3b8d3945c3c4b5e7372dba991d6d84a768b2f919d8411a`)
  and 400 validation rows (SHA-256
  `53994f411073b8ec156b3d51e474ec338cdaa3a8920f07e2aa28864fbc001f73`).
  Its manifest SHA-256 is
  `310707a15cb5497f2befdbc8fa2807f7a29a9c7deaa48fc3133261a83c4a987b`;
  it records `historical_inputs_used=false` and `test_split_opened=false`.
  Selected training spans cover 986 dates, 1,576 numbers, 1,347 units, 318
  legal sections, and 331 name/signature cues; validation has nonzero coverage
  in every category. It was retained as rejected preflight evidence and is not
  a training input because the later uniqueness audit found duplicate pairs.
- A direct parent-load preflight with PEFT 0.20.0 confirmed that
  `PeftModel.from_pretrained(..., is_trainable=True)` restores the frozen H1
  adapter over the pinned base with tokenizer size 64,400, rank 16, alpha 32,
  `inference_mode=false`, and exactly 5,996,544 trainable parameters. This is a
  continuation with a new optimizer, not a checkpoint or optimizer resume.
- The first protected augmentation (`short_aug_v2`) had 3,200 distinct IDs but
  only 3,168 distinct `(source, target)` pairs: 27 duplicate groups caused 32
  repeated pairs. The existing QAD primitive correctly rejected this instead
  of silently overweighting repeated examples. Prevention: keep the QAD
  uniqueness invariant; make the builder choose a deterministic alternative
  valid span/noise option on collision, preserve protected-value coverage, and
  bind a new create-once augmentation version rather than weakening QAD or
  mutating target text.
- The active collision-free augmentation is `short_aug_v3`. It has 3,200/3,200
  unique training pairs, 400/400 unique validation pairs, and 3,600/3,600 when
  combined. Bindings are: train 2,190,385 bytes / SHA-256
  `efe087fc97742370011447974348de5873690717b4f9a338280fd843c2508ec0`,
  validation 282,319 bytes / SHA-256
  `6c9ca30980e3e2ae6d068fef4ad674651abaa4349c6f46dcce655c3a9c529268`,
  and manifest 4,272 bytes / SHA-256
  `29a6e3ed62bd0123b0923a23784cbb93d59213ffb6039750c0f8e630fb4c6509`.
  The builder solved collisions only by choosing another eligible exact target
  span/noise option; it never changed target text or weakened QAD checks.
- A saved QAD working model is already resized from the base vocabulary of
  65,536 to the pinned tokenizer vocabulary of 64,400. Reusing the old
  `_validate_base_config()` after QAD would therefore reject a valid model only
  after expensive training. The recovery loader now has a dedicated regression
  seam that requires LFM2/16 layers/1,024 hidden units plus tokenizer, config,
  and both embedding tables at exactly 64,400 with tied embeddings. Prevention:
  validate each artifact at its own lifecycle state instead of applying a
  pre-resize base invariant to a post-resize saved model.
- The first short-recovery task launch exposed the same PRAXIST scheduler seam
  as the earlier Windows run: without task-local
  `SCRIBER_PRAXIST_SIMPLE_DIRECT=1`, a valid Windows evaluator can be recast as
  infrastructure exit 75. The new task copies the proven `praxist_shim` and
  protects it in `task.yaml`; Doctor, Resolve, Jinja, and both evaluator CLIs
  then pass. Start commands must place that shim on WSL `PYTHONPATH`.
- Full Python generation over 200 long plus 400 short cases is not an efficient
  Complete gate: the earlier 200-row PyTorch pass took about 5,357 seconds.
  The current PRAXIST task is therefore a terminal 12-long/24-short Canary and
  stays fail-closed with `short_recovery_pass=0`,
  `promotion_eligible=false`, and `qad_export_pending=true`. A separately
  receipt-bound Complete run performs the 600-case quality and loop/EOS/marker
  checks only after QAD-Q4_0 GGUF export through llama.cpp.
- A green configured-provider Doctor did not prove that a new PRAXIST agent
  could authenticate. The first short-recovery start failed before any model
  or GPU work because its isolated Codex runtime was not logged in. The proven
  route is to expose the existing Windows Codex state as
  `CODEX_HOME=/mnt/c/Users/Alexander.Immler/.codex` and start with
  `--codex-native`; the subsequent Doctor and launch both passed. Prevention:
  verify the exact start-time provider path, not only a different configured
  provider path.
- PRAXIST admits a fully specified GPU reservation only when observed use plus
  requested memory stays at or below 95 % of physical VRAM and observed plus
  requested utilization stays at or below 100 %. On the 8,188 MiB GPU, 651 MiB
  of unrelated observed use plus the original 7 GiB reservation was 7,819 MiB,
  just above the 7,778.6 MiB admission ceiling. Reducing memory alone did not
  admit the job because a profile without `gpu_utilization_pct` is treated as
  unknown and then requires an almost idle device (at most 256 MiB and 5 %).
  The measured recovery peak is about 5.6 GiB, concurrency is fixed at one,
  and the profile now explicitly reserves 6 GiB plus 50 % utilization so the
  one training job may share the Windows display GPU. Both rejected scheduler
  attempts had zero training steps. Prevention: specify both resource axes,
  size memory from measured peak plus bounded headroom, and inspect the actual
  scheduler queue before claiming that training has started.
- PRAXIST creates the canonical per-experiment result directory before it
  invokes the evaluator. Requiring the path itself to be absent therefore
  rejected a correctly admitted job before Windows or CUDA started. The
  evaluator and Windows orchestrator now share one rule: a missing path or an
  already-created empty directory is fresh; a directory containing any entry
  is rejected without overwriting it. Prevention: test orchestration contracts
  against the scheduler's real directory lifecycle, not only direct CLI runs.
- `safetensors` 0.8.0 exposes tensor names through `safe_open.keys()` but its
  context handle is not itself iterable. The first real parent preflight used
  direct iteration and failed before loading the model. A regression fixture
  now deliberately implements `keys()` without `__iter__`, and the live ML
  runtime preflight confirms all 184 finite, nonzero LoRA tensors plus the
  frozen directory and SafeTensors hashes. Prevention: exercise dependency
  boundary helpers once in the exact training environment, not only behind
  injected unit-test auditors.
- With `synthesis_trigger.enabled=false`, PRAXIST explicitly keeps peers alive
  until the generation runtime cap even after a terminal finding. The first
  successful Canary finished its model work and receipt in about 14 minutes,
  but the inherited three-hour cap would have added roughly 2.75 hours of idle
  control-plane time and prevented a finalized successful run receipt. The
  fixed one-arm Canary now has a 0.4-hour cap: enough margin over the measured
  end-to-end runtime while bounding finalization to 24 minutes. Prevention:
  size the outer agent lifetime as well as the inner training timeout, and do
  not assume a terminal peer notebook stops the generation controller.
- The product safety validator compares source text with model output and is
  intentionally conservative about changed numbers and legal references. It
  rejected the high-quality ground truth itself in all 200 long validation
  rows and in 24 of 400 v3 short rows, mostly because the desired editor must
  format spoken numbers, dates, units, and legal citations. It is therefore a
  diagnostic here, not a quality gate. The Complete gate instead compares each
  prediction with its target and requires zero mutations in the v3-protected
  date, number, unit, legal-section, and name/signature categories.
- A Canary result file alone is not enough authority for the expensive
  Complete run. The create-once Complete binder requires one finalized,
  successful PRAXIST run, one exact terminal export-ready finding with the
  canonical result path and SHA-256, and one closed scheduler containing only
  the successful Canary job. The sealed Complete receipt then binds that input,
  every data/order hash, base and H1 directories, and the exact SFT and QAD
  primitive source bytes. Prevention: bind the control-plane decision and the
  executable recipe, not only the resulting model directory.
- Complete training writes into a uniquely named sibling staging directory,
  validates the SFT adapter, QAD telemetry, full 148-tensor BF16 student, and
  sealed receipt there, then atomically renames it into the assigned PRAXIST
  result. Export likewise validates inputs and b10158 tools before and after,
  deletes the transient BF16 GGUF, and retains only the canonical QAD-Q4_0 GGUF
  plus its manifest. This prevents a partial expensive run from looking like a
  reusable Complete artifact.
- Full Complete evaluation reuses one persistent b10158 `llama-server` for the
  exact 200 long and 400 v3 short cases. It requires the long quality floors,
  Identity exact rate at least 0.995, Noisy exact rate at least 0.98, 100 % EOS,
  and zero loops, prompt-marker leaks, token-limit hits, or target-relative
  protected mutations. Even a passing Slice 2 remains recipe-regression
  evidence with promotion, release, publication, and the fresh product gate
  all fail-closed.
- The receipt-candidate Canary summary has SHA-256
  `f5ba22562f8a281eed48a12e54dc8158b0e95a5e5ff01b12ade2b772ef84f2f9`.
  Its 24 short cases split evenly into 12 Identity and 12 Noisy cases, with
  7/12 exact in each group and 14/24 exact overall. Of the ten failures, seven
  are early omissions at paragraph boundaries, one preserves an exact-word
  repetition, one changes a spoken comma command incorrectly, and one emits
  the complete target twice with `Bereinigte Fassung:` between the copies.
  All 24 generations reached EOS and none hit the token limit or looped, so the
  omissions are genuine premature EOS decisions rather than truncation. No
  protected value was substituted; five cases omit a protected date/time with
  their missing paragraph, and the marker-leak case duplicates its values.
  Prevention: retain explicit paragraph-boundary and copy-stability coverage,
  and keep zero omission, marker-leak, loop, token-limit, and target-relative
  protected-mutation requirements in Complete and fresh product evaluation.
- PRAXIST 0.5.0 does not finalize a successful fixed experiment merely because
  its scheduler job exits zero. With synthesis disabled, the peer waits until
  `per_generation_hours`; `max_generations: 1` applies only after that peer
  exits, and the stop command would record a stopped rather than successful
  run. The Complete task therefore enables synthesis for exactly one terminal
  finding, uses `min_interval_minutes: 0` and
  `mature_quorum_fraction: 0.0`, and retains the twelve-hour bound only as a
  fail-safe. `max_interval_minutes: 690` leaves 30 minutes of safety slack
  below that bound. Prevention: bind control-plane completion to the task's
  terminal finding instead of treating evaluator exit as run finalization.
- Two Canary runs with the same seed, frozen parent, v3 rows, sampler order,
  and byte-identical SFT adapter still produced different QAD models. Their
  first 16 microstep losses were identical, but the first observed gradient
  norm already differed and the first optimizer update then diverged; the QAD
  SafeTensors hashes were `fa541990fce9b7ecbcc5b1d6a79f9b7abd20b3e46bc90d7fca7b5e0fd20c03c6`
  and `42cf8dd78a60d4e199ef0ea397a90e9038ae2ac62c3a6ef38a26d9a069e9f0fa`.
  The resulting long score moved from 0.956365 to 0.933104 and the short score
  from 0.850765 to 0.817285. The QAD primitive had seeded only its DataLoader,
  leaving CUDA backward and optimizer reductions outside a deterministic
  execution contract. It now reseeds Python, Torch, and every CUDA device
  immediately before QAD CUDA admission, enables deterministic algorithms in
  hard-error mode, disables cuDNN benchmarking and both TF32 paths, and records
  the exact state in terminal telemetry. Both Windows evaluators prepare
  `PYTHONHASHSEED=17029` and `CUBLAS_WORKSPACE_CONFIG=:4096:8`, list every
  required child variable in `WSLENV`, and therefore pass them across the
  WSL-to-Windows process boundary before Python starts. Complete rejects any
  telemetry that differs from that exact CUDA contract. Prevention: require a
  two-process, same-input QAD hash replay before spending the full Complete
  budget; never treat a sampler seed alone as GPU training reproducibility.
- Canary quality floors are deliberately not a Complete-admission gate. The
  receipt-candidate Canary completed its exact 192-row SFT, 288-row fresh QAD,
  and 12-long plus 24-short evaluation with intact protocol, while its
  preliminary quality floors and `short_recovery_pass` remained false. The
  first Complete binder incorrectly required those Canary floors to pass and
  therefore rejected the real terminal `CANARY_READY_FOR_QAD_EXPORT` result.
  Admission now requires all 36 evaluation units, the frozen H1 parent, fresh
  AdamW short-only SFT, fresh QAD-Q4_0 student, one byte-bound terminal finding,
  and closed successful scheduler evidence, while promotion, release, and the
  fresh product gate remain fail-closed. Prevention: do not ask a Canary to
  prove the quality improvement that the all-data Complete stage exists to
  test.
- A finalized PRAXIST finding can serialize `source_result_path` as an absolute
  WSL mount path even when the Complete binder is running under Windows. The
  first binder accepted only the canonical run-relative POSIX path and therefore
  rejected the real terminal finding despite its correct result hash. The path
  contract now accepts either that exact relative path or an absolute drive/WSL
  spelling that resolves to the exact same existing file inside the exact run
  directory. It rejects non-drive absolute paths, missing files, paths outside
  the run, `.`/`..` traversal, and suffix-only lookalikes. The real finalized
  `run_2026-09-01_19-09-54-923101_fresh_windows_lfm2_350m_short_recovery`
  binder CLI then completed with exit code 0 against summary SHA-256
  `f5ba22562f8a281eed48a12e54dc8158b0e95a5e5ff01b12ade2b772ef84f2f9`.
  Prevention: normalize only explicitly supported Windows/WSL absolute forms,
  resolve strictly, and require both containment and exact file identity rather
  than comparing suffixes.
- The required two-process GPU replay passed after the deterministic QAD fix.
  Replay A and Replay B used the same seed and inputs but had materially
  different QAD wall times (164.674 s versus 288.053 s); nevertheless, both
  produced the exact QAD SafeTensors SHA-256
  `dcdddfb3fd43866cb1b0e3ef4973dcb0680b67ed5258825238c99a331c67db22`.
  The SFT adapter, optimizer and microstep loss histories, gradient norms,
  sampler evidence, update audit, protected review predictions, final scores,
  peak reserved VRAM (5.611328125 GiB), and final FP32-master hash
  `202f1a808d1e5d4f04aac45272eb07a8e5a04a0c0b77f599914e16ad94b87b01`
  also matched exactly. The differing summary-file hashes reflect runtime and
  path metadata, not model or prediction differences. The machine-readable
  comparison is cached at
  `praxist_task/scratch/qad_determinism_replay_20260901_comparison.json`.
- PRAXIST materializes one canonical result-artifact finding before the peer
  publishes the task-specific terminal finding. A synthesis threshold of one
  could therefore close the peer after `evaluation_summary.json` appears but
  before `CANARY_READY_FOR_QAD_EXPORT` is committed, leaving an otherwise good
  run unbindable. Both fixed one-experiment tasks now require two findings:
  the automatic result artifact and the explicit terminal finding. The
  generation time cap remains the fail-safe if either is missing.
- The first deterministic PRAXIST replay failed after SFT because Linux
  environment entries passed through `subprocess.Popen(env=...)` are not
  automatically visible to a Windows executable launched through WSL
  interop. A minimal `cmd.exe /c set CUBLAS_WORKSPACE_CONFIG` boundary probe
  reproduced the missing variable and passed once the name was added to
  `WSLENV`. Both Canary and Complete evaluators now preserve existing
  `WSLENV` entries, add each required deterministic/offline variable exactly
  once, and have regression coverage for the bridge. The Canary evaluator
  also returns exit code 1 after writing a canonical `protocol_failed`
  summary; otherwise PRAXIST could label a failed model protocol as a
  successful run. Prevention: test the real WSL-to-Windows boundary, not only
  the Python environment dictionary, and require nonzero process status for a
  terminal protocol failure.
- A direct post-fix Canary reproduction completed SFT and QAD before hitting a
  transient CUDA out-of-memory error during in-process validation. Its saved
  SFT SHA-256 was still exactly
  `3fbe35c9ef09032d7ddc608aeadfc1d2c61f3b754ae9892137ad5e0789994bab`,
  its QAD SafeTensors SHA-256 was exactly
  `dcdddfb3fd43866cb1b0e3ef4973dcb0680b67ed5258825238c99a331c67db22`,
  and its FP32-master hash was exactly
  `202f1a808d1e5d4f04aac45272eb07a8e5a04a0c0b77f599914e16ad94b87b01`.
  This independently isolates the repaired process bridge from model/data
  correctness. The failed run cannot authorize Complete; a fresh finalized
  PRAXIST Canary remains required.
- Disk cleanup removed 11.08 GiB of recomputable artifacts: deterministic
  replay directories, failed/obsolete short-recovery runs, historical QAD
  intermediate models and exports, old Boldt experiments, the historical
  runtime-E2E workspace, and three non-selected LFM variants. Free space on
  `C:` rose to 47.43 GiB. The fresh corpus, ML environment, Hugging Face base
  snapshot, QAD source primitive, frozen manifest, selected H1 adapter, and
  compact deterministic comparison JSON were retained; the H1 SafeTensors
  hash remained
  `08877206b8f0c4e46868cb27a8dcf2050dc7010f35c8044e25bf71282d0d4c0b`.
  These deleted artifacts are permanently gone and can only be recomputed.
- The fresh official post-fix Canary
  `run_2026-09-01_20-50-13-087948_fresh_windows_lfm2_350m_short_recovery`
  finalized successfully with 36/36 evaluation units, intact protocol and
  quality-floor evidence, combined score 0.900758, long score 0.984232, short
  score 0.817285, EOS 100%, no loops or token-limit hits, and one short marker
  leak. Its SFT adapter SHA-256
  `3fbe35c9ef09032d7ddc608aeadfc1d2c61f3b754ae9892137ad5e0789994bab`
  and QAD model SHA-256
  `dcdddfb3fd43866cb1b0e3ef4973dcb0680b67ed5258825238c99a331c67db22`
  matched both independent deterministic replays exactly. The canonical
  summary SHA-256 is
  `7862ef9b74881838918254e6339df661caed1481d60e88176544dc0adc4712d3`.
  A create-once Complete receipt now binds that summary and the closed PRAXIST
  run evidence under binding ID
  `50621880727b6160c3fe02a02a831c20db8073a5cfa1ef1bf705400bcc57f7fb`;
  the receipt file SHA-256 is
  `1a8087fe92a54253fcc97cad44f982f1df0a13721e38f76a76b59367c233cb55`.
  The receipt-bound Complete run started as
  `run_2026-09-01_21-01-11-282318_fresh_windows_lfm2_350m_short_recovery_complete`.
  Prevention: advance Complete only from this immutable successful receipt,
  never from a partial replay, deleted scratch run, or filename similarity.
- A second disk audit found 16 GiB below the WSL-only historical
  `/opt/scriber-polishing-v2/praxist-workspaces` tree. The entire payload came
  from ten retired `v13-resolve-*` and `v13-train-*` workspaces dated
  2026-08-31; one obsolete resolver runtime accounted for almost all 16 GiB.
  No running process referenced any of those exact directories, while the
  active Complete task and PRAXIST installation live elsewhere. All ten exact
  directories were permanently deleted, leaving the workspace root at 4 KiB
  and increasing free space inside the WSL filesystem by about 16 GiB. The
  dynamically allocated `ext4.vhdx` will not return those physical bytes to
  Windows until WSL can be shut down and compacted between training runs. WSL
  was stopped safely, but the available Sparse-VHD mode explicitly warned of
  possible data corruption and was rejected. A read-only DiskPart compaction
  could not start without an interactive elevated Windows approval, so no
  unsafe or partially authorized compaction was attempted; the VHDX remained
  33.70 GiB and Windows had 45.34 GiB free before the next run.
  Prevention: delete retired WSL workspaces after their receipts are preserved,
  then compact the stopped distribution; deleting files alone does not shrink
  a dynamic VHDX.
- The first receipt-bound Complete attempt completed all 3,200 SFT microsteps
  (200 optimizer steps) and all 4,800 QAD microsteps (300 optimizer steps), and
  successfully wrote its single QAD SafeTensors shard. It then failed before
  receipt creation, export, and evaluation because the post-training auditor
  required `model.norm.weight`. Real Hugging Face LFM2.5 full-weight models use
  `model.embedding_norm.weight`; the saved format otherwise had the exact 148
  tensors, 353,320,704 elements, BF16 dtype, 64,400-token tokenizer, one
  706,657,936-byte shard, and both layer 0 and layer 15 keys. The exact-key
  predicate now requires the canonical LFM2 key, has a red/green regression
  test, and passes a full audit against the preserved official Canary QAD
  artifact. The failure path correctly removed the whole unpublished staging
  tree, so the completed QAD weights and telemetry cannot be reused and the
  Complete computation must be repeated. Persisted failure evidence hashes are:
  summary `20025601baa922822a113311eab703ec6dd855dcebb033c7b59d0fae8dc2c922`,
  traceback `721834b30c6ebbb912fb96cd8c596c1805aac72cabf4211ce3da55fd8ccf52fd`,
  job log `435341fb28b5f1fe1869d72e6a9bbc4c7d97182e7449d3ec328ab475050d11a5`,
  and PRAXIST run summary
  `52af0960ad8e30dc0f06e0d4360f0be427a84048c8b1cb3542ad22c2660ccd17`.
  Prevention: integration-test every exact architecture-key predicate against
  one real saved artifact before starting a long run; injected audit DTOs do
  not validate the on-disk naming contract.
- The corrected receipt-bound Complete rerun
  `run_2026-09-01_22-23-15-882331_fresh_windows_lfm2_350m_short_recovery_complete`
  reproduced all 3,200 Short-SFT microsteps and 200 optimizer steps exactly.
  Its 287,793,184-byte `adapter_model.safetensors` has SHA-256
  `7b83dacf63303a03ef118f13b5faa40ce5d74d7242a4e4026d667b860a1172c7`,
  byte-identical to the unpublished first Complete attempt; its
  `adapter_config.json` also matches at
  `08bd5ffaf00e842d5eadbfa21d75caf29054ea72a24e27067cb4064404ab544d`.
  The telemetry file differs because it contains run path and duration data,
  while the loss series and model bytes remain deterministic. The fresh QAD
  phase then started with the repaired real-LFM tensor-key validator. This is
  progress evidence only until the terminal QAD export and 600-case evaluation
  finish successfully.
- The first Production task draft pinned the pre-fix byte hash of Complete's
  QAD model-contract module. Because Production validates every engineering
  source before training, it would have failed immediately with a source-hash
  mismatch even though the repaired validator itself was correct. A regression
  test now hashes the real active `complete_contract.py` and compares it with
  Production's allowlist; the allowlist is pinned to
  `b8b230e6a934df81fa77f5a30a2ec850484a6b3497e2f992701330e9f405fed3`.
  The focused Production suite passes 18 tests after the correction.
  Prevention: every cross-task source pin needs one test against the actual
  referenced file, not only loader tests that manufacture matching temporary
  hashes.
- The first synthetic product-lifecycle case containing the valid German
  amount `12.500,00 €` exposed a pre-existing safety-parser crash:
  `_digit_values` split on the grouping dot and attempted `int("500,00")`.
  The parser now accepts only strictly grouped mixed-separator decimals in the
  German `12.500,00` and English `12,500.00` forms, canonicalizes both to the
  same numeric value, and fails closed on malformed mixtures. Public manager
  coverage proves that the unchanged German amount is accepted; separate tests
  prove English preservation and rejection of one-cent mutations in both
  formats. The Local Polishing plus product-gate suite passes 177 tests after
  the fix. Prevention: final product cases must include locale-specific grouped
  decimals through the public `polish` path, not only isolated parser helpers.
- Adversarial review found that the first Production binder could accept
  minimal self-consistent fake Complete JSON: it checked status and copied IDs
  but did not revalidate the canonical Complete seals, trained-model bindings,
  real GGUF, or 600-case runtime package. The first Production receipt reader
  had the same trust-on-first-write problem for its own stage telemetry. Both
  boundaries now call the full canonical validators on every read, reconstruct
  the bound 2,000 Word parents, revalidate order, All-2,000 adapter, Short-SFT,
  QAD model, sampler and step evidence, b10158 QAD-Q4_0 GGUF, and all source and
  artifact hashes. The downstream product-gate binder is pinned to the exact
  hardened validator and dependency bytes. Minimal-fake and post-seal tamper
  tests were red before the change; the combined Production and E2E suite now
  passes 33 tests. Real default readback also passed for 2,000 reconstructed
  parents, 11 engineering sources, and the pinned base snapshot. Prevention:
  a receipt must be a cache of revalidated evidence, never authority by itself;
  every downstream consumer must replay the complete validation chain.
- The corrected Complete rerun successfully committed its full QAD model and
  receipt, then failed before evaluation because llama.cpp b10158 attempted to
  write the QAD-Q4_0 artifact to a 320-character Windows path and reported
  `ios_base::failbit set: iostream stream error`. The same retained QAD model
  exported successfully without retraining when the quantizer output path was
  shortened to 101 characters: the resulting 218,328,672-byte GGUF had SHA-256
  `e59007a418f9a490a59363c467c4ff55937acfe5953c9c8af66210c690f685c1`
  and exactly 92 Q4_0, 1 Q6_K, 55 F32, and 148 total tensors. The retained
  SafeTensors model has SHA-256
  `fda529a865b25a2d32bd2aff2a4012c9fc0516ff6329538d0b00c8713fde54b4`;
  its real `model.embedding_norm.weight` audit passed before export. The
  Complete receipt was therefore deliberately preserved for a receipt-bound
  export/evaluation recovery instead of repeating SFT and QAD. Prevention:
  invoke native Windows converters and quantizers only inside a short private
  workspace, validate there, then publish with Python into the potentially
  long receipt path; include a real path-length integration test.
- Adversarial review of the first fresh product gate found five evidence gaps:
  freshness could be asserted by timestamps and provenance booleans; a subclass
  of the real runtime factory could masquerade as product execution; process
  snapshots could miss late or reparented helpers and skip cleanup after a
  snapshot error; protected trees were measured before the model wrote its
  output; and malformed numeric text such as `1,2,3` was accepted as a safe
  mutation. Fresh-v2 now creates and byte-binds each post-freeze source, a
  manifest, payload hashes, and a denylist covering the 2,000 training pairs,
  600 Complete review cases, and five historical regression cases. Product
  evidence accepts only the exact SHA-pinned `runtime.py` and unchanged
  packaged `LlamaServerRuntimeFactory`, and it revalidates the full llama.cpp
  b10158 tag, commit, source archive, manifest, DLL, and EXE bytes. Native
  process evidence records PID plus creation time across prewarm, inference,
  close, and three post-close observations, including late, reparented, and
  PID-reuse cases; `close()` remains mandatory after a snapshot failure. The
  output must be a direct child of a new empty work root, disjoint from every
  protected tree, and all runtime, model, catalog, Production, and protected
  inputs are reopened and hashed after output creation. Malformed grouped
  numbers now fail closed while valid German and English grouping remains
  supported. The combined Gate, Local Polishing, and Production suite passes
  245 tests, Ruff, three CLI help probes, and a real Windows process-creation
  probe. Prevention: product evidence must bind behavior and live bytes, not
  self-declared provenance. Its explicit residual limit is novelty only against
  the bound inventories; it is not hardware attestation against a fully
  privileged host administrator.
- The first Production exporter still sent its final receipt-directory path
  directly to native llama.cpp, so it would have repeated Complete's failure
  after the expensive 2,000/4,000/6,000 training run. Production now enforces a
  240-character maximum for every native conversion/quantization path, creates
  BF16 and QAD-Q4_0 only in a short system-temporary workspace, validates the
  exact 92/1/55/148 tensor contract there, and publishes the verified bytes
  through an `fsync`-backed create-once hardlink into a tested final path longer
  than 320 characters. Review also found that read-time validation trusted the
  in-memory export manifest while opening only its artifact; a changed on-disk
  manifest or a newly sealed false manifest could therefore escape that layer.
  The validator now reopens the exact nonsymlink manifest filename, requires
  byte-identical content, exact fields, seal, fixed model/toolchain/QAD-only
  policy, artifact bytes, tensor counts, and deleted transient evidence.
  Temporary BF16/Q4 files and their directory must be gone after publication.
  The active Complete exporter pin is
  `c44516e4a3c8535db4b7c564832aa187197be08ea003e9cf231e986e2e85a136`;
  downstream Production and product-gate dependency pins are tested against
  live source bytes. Production plus E2E passed 53 focused tests, and the wider
  Gate, Production, and Local Polishing regression set passed 261 tests plus
  Ruff. Prevention: budget native Windows paths before launching a long run,
  then reopen every published manifest instead of trusting the object that
  produced it.
- The receipt-bound Complete recovery proved that the preserved QAD model was
  technically exportable and runnable without repeating training. Native
  llama.cpp b10158 rejected both a long quantizer output path and the later
  long `llama-server --model` path. The recovery therefore used byte-verified,
  process-lifetime hardlinks in short private directories for both boundaries,
  then removed those directories after close. On this hybrid-GPU Windows host,
  Vulkan also enumerated the NVIDIA device differently inside the isolated
  child. A verified physical-device remap plus child-only
  `GGML_VK_VISIBLE_DEVICES=1` and `GGML_VK_DISABLE_BFLOAT16=1` made the real RTX
  4070 path deterministic while leaving the parent environment unchanged. A
  clean-`PYTHONPATH` subprocess exposed another recovery-only import defect;
  loading the pinned exporter by exact file path with `importlib` fixed it
  without making task directories importable authority. The recovery suite
  passed 49 tests, Ruff, compile checks, and real server start/close. Prevention:
  test every native input and output path, isolated import path, and child GPU
  environment before an expensive run; retain only hash-bound receipts and the
  permitted QAD-Q4_0 artifact.
- The recovered QAD-Q4_0 artifact completed all 600 bound public cases, but the
  Short-SFT recipe failed scientifically. Short was 400/400 exact, while Long
  was only 6/200 exact with score 0.8654498409, structure 0.9029994048, and
  protected mutations in 114/200 cases. Of those mutations, 95 involved
  numbers, 24 units, 6 dates, and 1 a legal reference; 150/200 Long outputs had
  fewer paragraphs and 13 lost all list markers. All 200 outputs reached EOS,
  with zero loops, marker leaks, token-limit hits, or prompt truncations, and
  QAD KL remained low. This rules out the export/runtime boundary and strongly
  points to an overfit Short-SFT teacher: its loss was already about 0.004 at
  step 25 and approximately 0.0001 for most later steps. The run remains
  `quality_gate_failed`, with promotion, release, and publication disabled.
  Prevention: measure H1 and the Short-SFT teacher before QAD, then use a small
  H1-heavy high-precision interpolation sweep only if the teacher A/B confirms
  drift; run fresh QAD and the sole final QAD-Q4_0 export only for the selected
  teacher, never mix final GGUFs or blindly repeat the overshot continuation.
- Safety replay found a separate target-contract defect: the current product
  safety gate would reject all 200 ideal Long targets and the same 24 ideal
  Short targets it rejects from raw predictions. It would therefore return the
  source even when the model produced the bound correct answer. Source-vs-output
  safety remains useful for runtime diagnosis, but it cannot serve as a quality
  target when legitimate post-processing changes formatting, numbers, units,
  or modality placement by design. Prevention: define target-aware permitted
  transformations from the bound pair, test ideal targets as mandatory positive
  controls, and keep this product blocker independent of the model recipe.
- The first target-contract repair accepted all 600 bound ideal outputs (200
  Long and 400 Short) and still rejected the three observed numeric corruptions,
  but an independent adversarial review proved that positive replay alone was
  insufficient: it exposed fail-open month, unit, leading-zero, repeated-format-
  command, sentence-type, and content-addition mutations. The closed revision
  now accepts 600/600 ideal targets through rendering, direct Safety, and the
  product Manager helper; rejects 16/16 adversarial clusters through both direct
  Safety and Manager; and detects all three observed corruptions. The Manager
  repairs only the narrowly classified `changed_number` case (`word-1527`),
  fully revalidates that repair, and falls back to the source for `word-1644`
  and `word-1728`. The canonical 1,477-byte harness JSON has SHA-256
  `9319dd810bd59bda4111852e6e9facdd49c282980e0df7285280c58382a624f3`.
  Independent replay passed 65/65 focused tests, 252/252 scoped product tests,
  Ruff, and `git diff --check`. The implementation contains no corpus ID, hash,
  path, or expected-output whitelist; it uses general closed rules plus one
  generic `mietvertag`/`mietvertrag` typo equivalence. Warm CPU cost is now
  measurable: about 20 ms mean for Short and 98 ms mean for Long, with overall
  p95 about 136--139 ms, roughly 3.8 times the pre-hardening cost. That remains
  expected below 350M inference latency, but should be profiled with the final
  product candidate. The wider 4,479-test repository run still is not claimed
  green because two unrelated Meeting release-matrix tests invoke the unavailable
  Windows Store `python` alias (exit 9009). Prevention: require both the complete
  600-case positive replay and adversarial anchor-occurrence, leading-zero,
  month, unit, legal, modality, polarity, sentence-type, and content-addition
  negatives through the actual product path; never treat positive coverage as
  semantic proof by itself.
- The original run dashboard renderer correctly rejected the export-recovery
  summary because that summary contains no experimental `arms`; coercing it
  into the older schema would have fabricated evidence. The canonical
  read-only dashboard was subsequently narrowed to the finalized Teacher A/B
  evidence and its bound Recovery-QAD reference: H1, Short-SFT, and QAD now
  appear side-by-side for Long/Short score and exactness, with the -9.93-point
  SFT and -0.79-point QAD deltas rendered before the tables. The 7,637-byte
  artifact has SHA-256
  `50bd4a678720c6c31a2656f05928fc24279c99880fcb69e0c87f52c7ed26d46d`;
  the official portable builder produced a self-contained 402,894-byte HTML
  with SHA-256
  `1c812d349418cecf429aa1085aa2ec272cfc28dad36578fa9905d3820ba07bb4`.
  Official structure and payload validation passed. Direct Chromium rendering
  at 1440x1000 and 390x844 showed both tables, no horizontal overflow, and no
  console or page error; visual inspection passed. The plugin's combined
  delivery wrapper remained non-authoritative on this host, alternating among
  `reader_timeout`, `reader_not_visible`, and classic-scrollbar overflow for
  identical bytes. Prevention: never rewrite a recovery receipt into an
  incompatible dashboard schema; reconcile from finalized source evidence,
  keep the officially packaged and structure-validated artifact plus direct
  render proof, and do not report a false wrapper green for a known Windows
  reader race.
- The hash-bound two-arm teacher diagnostic isolated the Long regression before
  spending another QAD run. Both transient BF16 teachers completed the same
  fixed 600-case review and were then deleted. The retained H1 teacher scored
  0.9726618380 on Long with 169/200 exact targets, 1.0 structure, and 1.0 unit
  exactness, but remained unusable on Short at 53/400 exact with frequent loops,
  marker leaks, and token-limit stops. The fully merged Short-SFT teacher reached
  400/400 exact Short outputs and clean runtime termination, but Long fell to
  0.8733392160, only 4/200 exact, 0.8591952381 structure, and protected
  mutations in 103/200 cases. Relative to H1, Short-SFT caused a -0.0993226220
  Long-score change; the subsequent recovered QAD-Q4_0 changed it only another
  -0.0078893751. This classifies the failure as
  `short_sft_endpoint_dominant`, not a quantizer or runtime failure. No SFT or
  QAD step ran in this diagnostic, no artifact became promotion-eligible, and
  both approximately 709 MB BF16 GGUF files plus both merged HF workspaces were
  removed after their hashes and predictions were sealed. Prevention: never
  repeat the alpha=1 Short continuation or blame QAD for this regression; first
  search the H1-to-Short adapter direction with a small H1-heavy full-weight
  teacher sweep, reuse the fixed 600 cases, and run fresh QAD only once for a
  teacher that passes both Long and Short gates. PRAXIST finalized this run as
  `succeeded` with exit 0 and 1,200/1,200 evaluation units. The canonical
  evaluation summary has SHA-256
  `2a01ea06c7c901bfc65095dc649c252cb548098ca056ef08ce1bafe122b680ca`,
  the run summary
  `7bc9a2b79e9cc39b47bda4442eec2bbed94c8c047b454a2f41908cf741b4ad0e`,
  and the final orchestrator status
  `7b9fc72a6d262750514553050bfa52d592857ac404018eaa1cac3d4280a86f8b`.
- A post-cleanup size audit found 43.35 GiB free on `C:` and about 940 GiB
  free inside `Ubuntu-24.04`. The apparently old
  `praxist_task/fresh_windows_350m/.venv-win` still occupies about 4.5 GiB, but
  it is not a disposable Boldt run: the active LFM dataset/setup scripts resolve
  their Windows Python from that exact shared environment. It was therefore
  preserved while empty old run directories remained harmless. Prevention:
  trace live path references before deleting a large directory whose name looks
  historical; reclaim experiment outputs and transient models first, not a
  still-referenced shared runtime.
- The fixed full-weight Teacher sweep evaluated alpha 0.25, 0.50, and 0.75
  between the sealed H1 and Short-SFT endpoints on the same 200 Long plus 400
  Short cases. Alpha 0.25 preserved every Long target floor (score
  0.9735057836, exact 169/200, structure and unit exactness 1.0) but Short was
  only 241/400 exact, with Identity 0.68 and Noisy 0.525. Alpha 0.50 was the
  closest interior arm: Long score 0.9644206884 and Short score 0.9989983798,
  but it still missed Long critical exactness (165/200), whole-output exactness
  (157/200 versus the 164 floor), structure, unit exactness, and Short Noisy
  exactness (190/200 versus the 196 floor). Alpha 0.75 made Short 400/400 exact
  but reduced Long score to 0.9330206356 and whole-output exactness to 54/200.
  No interior arm passed both gates, and the sealed endpoints show the same
  monotone trade-off; QAD remained forbidden. PRAXIST finalized successfully
  with one scheduler completion, zero failures, exact endpoint identities, and
  no retained BF16/HF workspace or model process. The canonical evaluation
  summary SHA-256 is
  `b5b36ce0a0925f8e3e86e11d6b7e16526bdf4de14ce307aeec586bb7cc23200f`,
  run summary
  `bfcbebf7b5d4e4f77c5abd35b997f750405e7cc0642485af3dafc0a3f8920d10`,
  and final orchestrator status
  `5d786d2297f666845b2d61f5cd5ef882ee2cf1ed69c28dc077f262a5fb54693b`.
  Prevention: do not spend another QAD run or a finer interpolation sweep on
  this Pareto conflict. Train Long and Short together with deterministic
  per-window rehearsal so every optimizer update sees both behaviors, then
  evaluate that BF16 teacher before QAD.
- The first PRAXIST launch of the fixed Mixed-Rehearsal task failed before model
  loading or any optimizer step. The WSL evaluator correctly used
  `/mnt/c/.../python.exe` to start the outer Windows Job-Object wrapper, but then
  reused that Linux path as the nested executable passed to Windows
  `subprocess.Popen`, which rejected it with `WinError 2`. The failed run
  `run_2026-09-02_02-55-51-773311_fresh_windows_lfm2_350m_mixed_rehearsal`
  finalized with zero completed experiments and one failed infrastructure job;
  its final orchestrator SHA-256 is
  `21b3b9775e60b9c8b17a44a58f340fbc169a1d17d4b85dab2c0c837fb7b88823`
  and run-summary SHA-256 is
  `9280f91e17ceb62a7b608b733691b0854fd622fca3503a87aea71a94d1464afb`.
  It is not model-quality evidence. The bridge now carries two explicit values:
  the outer WSL launcher remains `/mnt/c/.../python.exe`, while only the nested
  child is converted to `C:\\...\\python.exe`. A red-to-green regression test,
  12/12 scoped tests, whole-task Ruff, diff checking, and two real
  WSL-to-Windows-wrapper-to-Windows-child smokes confirmed `job.ready=assigned`,
  exit 0, and no surviving owner process. The unchanged bound task then started
  as the new PRAXIST run
  `run_2026-09-02_03-05-07-352691_fresh_windows_lfm2_350m_mixed_rehearsal`;
  its first real training evidence was optimizer step 1/400, loss
  `0.06749667583062546`, with about 3.45 GB GPU memory in use. Prevention: every
  cross-OS process-tree contract must test the outer executable in the caller's
  path syntax and the nested executable in the child's path syntax through one
  real bridge smoke before an expensive launch; a pure Windows-path unit fixture
  cannot prove WSL interop.
- The canonical LFM dashboard now includes the finalized five-point Teacher
  Alpha trade-off without changing the earlier A/B and Recovery evidence. Its
  Alpha metrics come only from the sealed sweep evaluation plus the three
  sealed interior summary/prediction pairs; alpha 0 and 1 are labelled as
  endpoint references, while alpha 0.25, 0.50, and 0.75 are labelled as
  diagnostic interior arms. The chart and table state that no arm passed both
  hard gates and make no global Pareto claim. Official artifact validation and
  embedded-payload structure checks passed with 1 chart, 3 tables, 6 blocks,
  5 Alpha rows, 10 chart rows, and all source IDs preserved. Direct Chromium
  152.0.7977.66 rendering passed at 1440x1000 and 390x844: the portable reader
  reached `ready`, the required Alpha/Recovery text remained visible, page
  width matched viewport width, and console/page errors were both empty. A
  separate JavaScript-disabled mobile render also passed with all five exact
  German Alpha labels (`α 0,00` through `α 1,00`), five rows, no corrupted
  25/50/75/100 labels, and no page overflow. The final `artifact.json` is
  21,576 bytes with SHA-256
  `5bb797b403ed632d9fc2ae144d34bf791d1dc1282fcba1234d043a28e398f033`;
  the self-contained `latest.html` is 431,980 bytes with SHA-256
  `f8aba84a449e9545311f0355c98b48a22a6f9a085791663e8308087d44ee1f88`.
  Prevention: keep future Alpha dashboard updates bound to sealed summaries
  and predictions, distinguish endpoint references from executed interior
  arms, and use official artifact validation plus direct desktop/mobile render
  evidence without retrying the known unreliable combined delivery wrapper.
- The first real Mixed-Rehearsal timing sample proved that the original
  120-minute infrastructure timeout was too short even though training itself
  was healthy. Steps 10, 20, and 30 arrived at 05:09:06, 05:12:30, and 05:15:54
  local time, so both ten-step blocks were stable at about 20.4 seconds per
  optimizer step. That projects roughly 136 minutes for 400 steps before model
  saving, BF16 merge, and the 600-case evaluation. The run
  `run_2026-09-02_03-05-07-352691_fresh_windows_lfm2_350m_mixed_rehearsal`
  was therefore stopped deliberately at step 30 rather than allowed to waste
  two hours and be killed at its inevitable deadline. Its Windows Job tree was
  removed, GPU memory returned to 302 MiB, no adapter/SafeTensors/GGUF survived,
  and it completed zero evaluation units. The final orchestrator SHA-256 is
  `9d84380be94a876c7a2beebb57aff358047cf153206344ddb6169a758ba45a6f`;
  the run-summary SHA-256 is
  `c3aeed5967dbff9d372483f3ad4771b5ca78c28a8cddc12f4ef1d7f7897df2d3`.
  Only infrastructure ceilings changed: evaluator and protected-PID waits are
  now 14,400 seconds, PRAXIST generation runtime is 4.5 hours, and synthesis cap
  is 240 minutes. The recipe, seed, deterministic order SHA-256
  `5c79bd3918caa9017ff2c5677f2f7001dd6e4f1746613adf4fb9f46de8001664`,
  H1 parent, and data are unchanged. Thirteen scoped tests, whole-task Ruff,
  compilation, Doctor, and Resolve passed before the replacement run
  `run_2026-09-02_03-21-22-965067_fresh_windows_lfm2_350m_mixed_rehearsal`.
  Its step-1 loss was again exactly `0.06749667583062546`, providing a direct
  determinism check across restarts. Prevention: derive expensive-run timeouts
  from at least two stable measured progress intervals and include merge/export/
  evaluation plus a conservative margin; timeout-only changes must never be
  described as a new training experiment.
- The five failed historical Windows product-E2E cases are not lost. Their
  complete source strings and runtime outputs survive in the local Codex
  session log
  `rollout-2026-09-01T18-29-10-01a05dcd-9487-7782-9eb5-55c7ee3398c4.jsonl`,
  whose currently verified SHA-256 is
  `87b1e75ad6db4b0f46ab37530a733779c41951af49a92ea2508cacc9d406ba35`.
  The three Short sources are fixed at JSONL line 84 / ordinal 83 and their
  outputs at lines 92--93; the two Long sources are at line 214 / ordinal 213
  and their outputs at lines 215--216; the final 0/5 report is at line 313 /
  ordinal 312. Their normalized fingerprints are
  `a697df2c90093d458587bcc53ea6bdb184c55c790d99bcc8b94b834a3451e880`,
  `c7a874f5c5ace636f728cf559c21303e6de2c694db978a13cb9affdc577fe01e`,
  `c2c1792784eb159a44bb850b0a279841ca79fd8d6bc841cd2540a21bcded47de`,
  `539db4e333814d88d5dbf9f73fdbeb29a312840bcaef96cf0543cc0c8cb94c94`,
  and `b19af9ca8a30f36d9d2ec55b1b4c1ffaf123bc7beacd411a1346755b4c9b383a`.
  Exact-fragment searches found none of these sources in the active repository
  or corpus inputs, while Git, GitHub, Actions, and the deleted temporary E2E
  root yielded no separate receipt. Prevention: restore the historical group
  only from this byte- and ordinal-bound primary evidence, retain it solely as
  forbidden fingerprints and optional historical regression, never feed it to
  Teacher/SFT/Short/QAD, and generate the separate final product cases only
  after recipe freeze with explicit disjointness checks.
- The first historical-five recovery implementation passed synthetic tests but
  failed its first real-log smoke before writing any binding. The selected
  JSONL record stores a JavaScript tool wrapper in `payload.input`; its nested
  PowerShell `cmd` is itself JSON-encoded, so the initial parser saw literal
  backslash-`n` bytes after `@'` instead of the decoded line feed expected by
  the here-string parser. The corrected recovery path first requires exactly
  one pinned `tools.exec_command(...)` wrapper, JSON-decodes its sole argument,
  checks its exact field set, work directory, yield, and output limit, and only
  then parses the embedded Python with `ast`/`literal_eval`; no wrapper or log
  code is executed. The revised suite passed 9/9 focused and 40/40 complete
  product-gate tests, Ruff, compilation, and a second real CLI smoke against
  the pinned 10,092,199-byte session log. That smoke produced exactly five
  historical fingerprints, validated cleanly, and its 1,817-byte temporary
  output was deleted. Prevention: fixtures for layered command logs must retain
  every real serialization layer, and any recovery parser must pass one smoke
  against the exact immutable source bytes before its synthetic green result
  is accepted.
- The first attempt to extend the Windows keep-awake helper requested 43,200
  seconds, but the existing reviewed script intentionally accepts at most
  28,800 seconds and therefore exited immediately. The earlier 28,800-second
  owner was stopped only after the replacement had appeared live, which left a
  short gap but did not interrupt the active Windows Job or CUDA worker. A new
  hidden helper was started with the permitted 28,800 seconds and independently
  verified alive as PID 249244 at 07:21 local time. Prevention: read and honor
  a helper's parameter validation before replacing its live instance, and
  verify the replacement again after at least two seconds rather than relying
  on the initial process handle alone.
- The terminal Mixed-Rehearsal Development run
  `run_2026-09-02_03-21-22-965067_fresh_windows_lfm2_350m_mixed_rehearsal`
  reproduced step 1 exactly, completed all 6,400 microsteps and 400 fresh
  AdamW optimizer steps, then evaluated the transient BF16 Teacher on the bound
  200 Long plus 400 Short cases under llama.cpp b10158 Vulkan. PRAXIST ended
  `succeeded` with one completed attempt, exit code 0, zero failed, recovered,
  or rejected jobs, and closed admission. The Teacher was close but did not
  pass: Long score was `0.9981535251274865`, exact match `0.985` (197/200),
  structure `0.9998125` against the immutable `1.0` floor, while Short exact
  match was `0.9975` (399/400), identity exact `1.0`, and noisy exact `0.995`.
  The four exact misses were two Long content-preservation errors
  (`word-0538` repeated `45 Uhr um`, `word-0666` changed `48.415` to
  `48.515`) and two removed paragraph boundaries (`word-1921` Long and
  `word-0526::short_noisy::spoken_format_command` Short). Consequently
  `all_teacher_gates_passed=0`, QAD remained ineligible and unrun, and the
  diagnostic adapter tree SHA-256
  `3b943833a758ed246b551b9f2824074450acd73e285b257a773fed197d96188c`
  was not retained. The canonical evaluation summary SHA-256 is
  `32321b9f6fb058492cbfd53d9ddece7cc104912e20c308e9dee5bfed4dbb2228`,
  runtime summary SHA-256
  `b9676f9382be6e948d7d814710fbdc04f9d7b7bd408b4870e32e833bcc40a234`,
  predictions SHA-256
  `86932c266a55c53780477c7d02c37a148f6fb502840a541c22d2675eecf37d03`,
  and run-summary SHA-256
  `41c7b85d0558ed55d20b3b85a6ef67cc8d1762051fdd28f81449d353ece95f97`.
  No llama-server, CUDA worker, candidate adapter, transient BF16/GGUF, or
  `C:\sm\mixed-rehearsal\run-*` workspace survived. Prevention: do not weaken
  the structure gate and do not start Production or QAD from this near-pass;
  bind these exact four residuals and all 596 exact outputs when evaluating a
  generally justified successor, without opening the held-out test split.
- PRAXIST's auto-materialized canonical-result finding for that run recorded
  `source_result_sha256=32398ac4...`, which does not match the unchanged result
  file's independently repeated SHA-256 `32321b9f...`. The peer-authored
  terminal finding, generation boundary, experiment ledger, and direct file
  hash all agree on `32321b9f...`. Prevention: never use the auto-materialized
  `source_result_sha256` alone as a Development binding; require the actual
  file binding plus the peer-authored terminal finding, scheduler status,
  run summary, and semantic `summary_id`.
- On 2026-09-02, after reviewing the exact 197/200 Long and 399/400 Short
  outcome and the four residual differences, the repository owner explicitly
  accepted that precision as sufficient and declared the Development gate met.
  This is an owner acceptance of a measured near-pass, not a rewrite of the
  automated result: `target_quality.passed`, `long_gate.passed`, and
  `all_teacher_gates_passed` remain false, and the `1.0` structure floor remains
  unchanged in the immutable Development evidence. Production is nevertheless
  authorized by a separate owner-acceptance receipt bound to this exact run,
  summary, metrics, and residual set. Because the failed Development adapter
  was correctly deleted, Production must still regenerate the exact All-2,000
  parent and train the fixed Mixed recipe from that parent; it must never infer
  authorization to use a missing or reconstructed Development adapter as its
  parent. Prevention: represent a human quality decision as an explicit,
  auditable override beside the automated evidence, never by changing a gate
  threshold, result field, or historical artifact.
- The prepared Mixed Production boundary now proves the owner override and the
  automated result as two separate immutable facts. Its Development v2 binding
  accepts only the exact run, byte-pinned summary/runtime/prediction evidence,
  596 exact outputs, and four named residuals; `production_authorized=true`
  comes only from the separate sealed owner receipt while every automatic gate
  remains false. Production reconstructs the 8,000-entry Mixed schedule from
  the bound 2,000 original and 4,000 short rows, recreates the exact All-2,000
  parent, and reconstructs both the 6,000-row QAD input shuffle and the actual
  seeded DataLoader consumption order. The QAD telemetry now binds the Mixed
  teacher before and after training, the fresh student model, and all real
  available, consumed, and microstep counters at 6,000. The final export is
  checked by the existing full post-export auditor and every one of the 600
  runtime predictions is non-empty and bound to its exact case set, GGUF, and
  export manifest. The evaluator retains the final sibling artifact path,
  publishes only after reopening and recursively checking all bindings, removes
  every partial stage on failure, and terminates the Windows Job tree for any
  `BaseException` after process creation without killing a normal exit-zero
  process. Thirty-three scoped Python 3.13 tests, isolated Ruff, compilation,
  PRAXIST Doctor, and Resolve passed without starting training, QAD, binding,
  or Product E2E. Prevention: test both ordinary nonzero exits and a live
  `BaseException` path, and require post-publish reopening so atomic publication
  cannot leave a receipt pointing at a moved or temporary path.
- A real create-once smoke against the accepted Mixed-Rehearsal run exposed
  three historical-format details that synthetic full-binding fixtures had
  hidden. `run_mixed_rehearsal.py` sealed its terminal summary with canonical
  JSON plus a trailing newline, while the newer Mixed Production contract does
  not append that newline. The Development binder therefore verifies this one
  legacy summary with the exact historical algorithm and binds the byte-pinned
  producer `run_mixed_rehearsal.py`; the new contract remains unchanged. The
  summary also stores only `{size_bytes, sha256}` for `training_order.json`,
  `runtime_summary.json`, and `predictions.json`. The binder now derives only
  those three fixed siblings from the already pinned diagnostic directory,
  compares each reduced identity to the live file, and emits the full verified
  binding without searching. Finally, the peer-authored terminal finding does
  not claim a summary path or 600-case count; it is selected by its exact pinned
  finding ID, claim, variant, peer, and false/true gate metrics, while the
  600-case and summary-byte evidence are validated independently. A real
  temporary owner receipt and Development v2 binding then created and fully
  revalidated with `automated_development_gate_passed=false` and
  `production_authorized=true`; both temporary files and their directory were
  deleted, and no task input or run was created. Prevention: mirror historical
  producer formats in fixtures, derive reduced legacy references only through
  fixed sibling names, and never attribute evidence to a finding that the
  finding itself does not contain.
- On 2026-09-02 the owner moved Production compute to Mithril and selected the
  cheapest live Spot option: one H200 141 GB SXM5 in `us-central2-b`. The live
  API reported a USD 1.00 spot price and available capacity, so the task uses a
  USD 1.01 per-instance-hour limit, one node, one GPU, and a 15-minute idle
  autostop. The local PRAXIST run started immediately before that instruction
  was terminated cleanly with all five recognized processes gone and no CUDA
  worker remaining. The fixed LFM recipe and sole fresh Word source did not
  change. Prevention: when the compute provider changes, stop the previous
  owner before provisioning the replacement and keep price, region, GPU count,
  autostop, and data identity explicit in the remote receipt.
- Mithril CLI `0.1.0rc3` was installed under WSL and its credential file was
  restricted to the user; no API secret is stored in the repository, task
  YAML, uploaded workdir, or receipt. `ml setup --check` confirmed the API key,
  project, and billing. The first remote setup used the image's Python 3.10 and
  failed before training because the pinned RapidFuzz 3.14.6 requires Python
  3.11 or newer. The second installed Python 3.12 with `uv` but created an
  unseeded virtual environment, so `pip` was absent. The corrected setup uses
  `uv python install 3.12` plus `uv venv --clear --seed --python 3.12`; it then
  installed the exact Windows-development package versions on CUDA 12.8.
  Prevention: do not infer the task interpreter from the host image label, and
  seed a `uv` virtual environment when subsequent commands intentionally use
  `pip`.
- The first remote program invocation downloaded exactly the six files of the
  pinned LFM2.5 350M snapshot and stopped before opening the corpus because the
  expected filename tuple placed `tokenizer_config.json` before
  `tokenizer.json`, contrary to bytewise sorting. Direct inspection confirmed
  there were no extra snapshot files; only the assertion order was wrong. The
  tuple was corrected, the completed environment was reused with `ml exec`,
  and job 4 reached `stage_started=all_2000_sft` on the H200. Prevention: when
  checking a sorted filename tuple, construct the expected tuple in the same
  sort order or compare sets plus an explicit file count; do not diagnose extra
  files before listing the live directory.
- Mithril job 4 completed the fresh All-2,000 SFT stage and sealed its adapter
  as SHA-256 `984d4fa4cf483eae44832049348373594f032a59d41d7b92719de816cb7c99c9`.
  Short augmentation also completed, but Mixed SFT stopped before its first
  optimizer step because dynamically loading `train_qad_lfm2.py` does not add
  that file's directory to Python's module search path; its sibling import
  `qad_q4_0` was therefore unavailable. The remote entrypoint now adds the
  exact operator directory before loading any bound training module. The
  completed create-once stage markers remain intact, so the restart validates
  and reuses All-2,000 plus augmentation rather than training them again.
  Prevention: every dynamically loaded module with sibling imports needs its
  own exact directory on `sys.path`, and a remote packaging smoke should import
  all bound modules before expensive training begins.
- The local Mithril return path now has a dedicated fail-closed importer. It
  rejects absolute paths, traversal, links, devices, Windows-reserved names,
  case collisions, duplicates, and oversized archive members; extracts only
  `outputs/` and `state/` into a private staging directory; verifies the sealed
  compute receipt, exact source/model policy, every stage inventory, and every
  completion marker; then atomically publishes the local tree and a separate
  sealed import receipt. This keeps the remote receipt byte-identical while
  rebinding verified stage bytes to their Windows locations. Prevention: never
  use a generic archive extraction or trust remote absolute paths when bringing
  a trained model back to Windows.
- Mithril job 5 resumed the sealed All-2,000 and augmentation stages, then
  completed all 500 Mixed optimizer steps. The completed Mixed stage is bound
  as SHA-256 `b0817d3a2cc6e9daef730ed6b7cb64b045db106f7e9155fc0d8ce04fb93bc759`;
  QAD then started from that teacher. The safe Windows importer passed five
  focused tests covering a valid round trip plus traversal, reserved-name, and
  symbolic-link rejection. Prevention: distinguish a quiet QAD inner loop from
  a stalled job by checking the exact remote PID and GPU allocation; do not
  restart merely because the primitive emits no per-step progress lines.
- The existing native-Windows ML environment under the fresh task contains
  the exact Torch 2.11.0 CUDA 12.8, Transformers 5.16.1, and PEFT 0.20.0 stack.
  Before the remote QAD result arrived, the Windows completion path validated
  all 32 byte-bound engineering inputs and compiled cleanly. Its local phase is
  intentionally limited to validating the imported student, exporting the sole
  b10158 QAD-Q4_0 artifact, and running 600 persistent-server cases; it cannot
  start SFT or QAD training. Ruff passed on all four Mithril helper sources and
  the safe-import suite remained 5/5 green. Prevention: preflight the native
  export environment while remote compute runs, but keep remote training and
  local product-runtime ownership separate and explicit.
- Copying completed telemetry through nested PowerShell, WSL, and SSH shells
  exposed a path-handling trap: a shell variable expanded in the wrong layer,
  so the first transfer returned without creating the intended Windows file.
  Repeating the transfer with explicit absolute source and destination paths
  produced all four cache files, and every local SHA-256 matched the remote
  value. Prevention: use literal full paths across nested shells, then require
  the destination file and its remote checksum before treating a transfer as
  complete.
- Mithril job 5 completed QAD after all 6,000 unique examples and 375 optimizer
  steps. Its QAD stage is sealed as SHA-256
  `d6be2d199c941e0da5e927bdf8138a0a66219337ff7a4c3b2ae51a54f6ae4a14`.
  The 976 MB return archive matched SHA-256
  `55247376c10add7b3bd862fa6bdac05bea910e0313db66cd961f8cd8a45320bf`
  on Mithril and Windows, then passed the stricter per-stage importer and
  created verified import ID
  `ec0461f8e894554edae3c3c1c4f3850bbd2abd37b2e934665b164b7539292725`.
  Only after that verification was the H200 Spot cluster terminated.
  Prevention: retain paid compute until the archive checksum and local sealed
  import both pass, then terminate it immediately.
- The first local 600-case runtime attempt exported the QAD-Q4_0 GGUF but failed
  before producing predictions because `_ChildEnvironmentRuntime` reused the
  core `generate()` method without forwarding its newly required `_tokenize()`
  helper. The wrapper now forwards tokenization to the same core runtime, and a
  focused regression test proves that delegated generation can call it. The
  create-once completion wrapper removed the partial export directory before
  retry. Prevention: when reusing an unbound class method through a wrapper,
  forward every internal method that the reused implementation calls and cover
  the delegation with a unit test.
- The first successful Mithril QAD candidate passed the 200 Long plus 400 Short
  Windows recipe regression exactly, with no loop, prompt leak, token-limit
  stop, protected mutation, or safety rejection. Median latency was about
  687 ms for Long and 196 ms for Short on the local RTX 4070 Laptop GPU. It was
  nevertheless rejected as the terminal Production artifact because its fresh
  All-2,000 adapter did not match the older frozen adapter identity. Prevention:
  keep model quality and protocol identity as separate gates; a perfect runtime
  score does not authorize rewriting a frozen parent binding.
- A direct local attempt to recreate the historical All-2,000 directory hash
  completed all 125 optimizer steps but produced a different fresh adapter and
  was removed by the fail-closed wrapper. The old hash is therefore not
  reproducible with the current bound code and environment. The active resume
  now uses the already verified fresh All-2,000 stage from Mithril job 5 rather
  than spending more compute chasing historical bytes. Prevention: perform one
  bounded reproducibility attempt, preserve the observed mismatch in the
  journal, then freeze an exact current artifact instead of looping.
- Cross-platform directory hashing exposed a separate defect: Python's native
  `Path` ordering placed `README.md` differently on Windows and Linux, changing
  a directory digest while every file byte was identical. The resumed task
  sorts by each relative POSIX path explicitly; its portable All-2,000 adapter
  SHA-256 is
  `cc29a35b0cb680c333abfa9028786a82381c0e642722b9bd4e3f7550e55ba04c`.
  Prevention: directory manifests must sort normalized relative strings, never
  platform-specific `Path` objects.
- At the owner's request, the next Mithril attempt uses an explicit
  `h200_full_capacity_v1` execution profile instead of the portable batch-one
  implementation. Mixed SFT presents one complete 16-example schedule window
  per batch with no gradient checkpointing. QAD presents 16 examples per batch,
  retains the same 16-example optimizer window, disables checkpointing, and
  expands Q6/KL chunks. Per-example losses are still averaged equally, the
  shuffled order and 500/375 optimizer-step contracts remain intact, and the
  only deployment target remains QAD-Q4_0. Prevention: record physical batching
  separately from the logical optimizer recipe and never claim that allocating
  unused VRAM is useful work.
- The full-capacity Mithril retry completed successfully. Mixed SFT consumed all
  8,000 scheduled examples in 500 optimizer steps with batch 16, no gradient
  checkpointing, and a measured 50.97 GB peak reservation. QAD consumed all
  6,000 unique examples in 375 optimizer steps with batch 16 and a measured
  44.04 GB peak reservation. The job sealed Mixed stage SHA-256
  `77f6fd3f1e6163e30da70d31fee9b8dade6ac1d2fba02a5533e44f4effbe5e85`
  and QAD stage SHA-256
  `d55a0da36319a6c9a8c48ba4246712cf620f9477c6ea6259115d688d15604e7c`.
  Its 1,023,221,210-byte archive matched SHA-256
  `10cb6b43fff9c869478c23e5f6f687a0df4484479a348e15e608bdffe1554f39`
  on Mithril and Windows, passed the safe importer with import ID
  `b2b7a7f82cfbc9250c97b7e27acacce533ea0497a553cc31d3830beffeda705e`,
  and only then was the Spot cluster terminated. Prevention: make
  `h200_full_capacity_v1` the default profile for later H200 work, preserve the
  logical 16-example optimizer windows, and always terminate paid capacity only
  after remote/local hash equality plus the sealed safe import.
- The full-capacity H200 artifact reached 599/600 exact runtime cases. Its sole
  miss changed protected value `3.001 €` to `3.010 €` in `word-0912`, so it was
  not selected despite using more of the H200. The earlier Mithril job-5 QAD
  artifact remains the quality winner at 600/600 exact. Prevention: use full
  H200 capacity for future training speed, but select by output quality rather
  than VRAM utilization; different physical batching can change final weights
  even when logical example windows are preserved.
- The owner explicitly accepted the measured precision and then removed the
  requirement for a sealed environment or additional signed-receipt chain. The
  unfinished fresh-product receipt construction was stopped immediately. The
  release path now stays simple: public immutable model, normal Scriber install,
  real local completion, and ordinary automated tests. Prevention: do not
  restart the abandoned receipt gate unless the owner asks for it again.
- The selected QAD-Q4_0 GGUF was published publicly at immutable Hugging Face
  revision `d64f8a14a09b2916000d969edd18bc411745e53a`. Anonymous reads verified
  the 218,328,640-byte GGUF SHA-256
  `e1ca3391d896db64df91c5ed5a02e16f5b6bbec5de81667ec99535eb7b1c0486`
  plus its policy, manifest, license, and modification notice. The model card
  and notice retain `Praxist by Sapient Intelligence`. Prevention: publish only
  the chosen QAD artifact, never a BF16/Q8/PTQ alternative, and test access
  without a Hugging Face token before wiring the catalog.
- The first direct runtime-preparation command used an arbitrary output folder
  name and correctly failed because the preparation tool owns only a directory
  named `local-polishing`. Re-running under
  `C:\sm\scriber-lfm-public-runtime\local-polishing` succeeded. Prevention: use
  the packaging tool's owned leaf name instead of treating its output parameter
  as an unrestricted temporary directory.
- The real Scriber manager then downloaded and verified 218,347,894 bytes
  anonymously into the user's managed model cache, prewarmed the packaged
  llama.cpp b10158 Vulkan runtime, and accepted four new everyday dictation
  checks. Latencies were 307.1, 322.6, 575.0, and 417.3 ms on
  `vulkan_compat`; spoken punctuation, a salutation paragraph, German currency
  and area units, and an unanswered question were all rendered correctly. The
  manager closed without leaving a `llama-server` process. Prevention: validate
  the actual download-manager-runtime path, not only a direct GGUF evaluator.
- A first focused test command used Python 3.13 and failed during collection on
  the repository's valid Python 3.14 parenthesis-free multiple-exception syntax.
  Re-running through `scripts\project-python.cmd` used Python 3.14.7 and passed
  all 364 focused backend tests. The focused React component test passed 7/7
  and both frontend TypeScript projects compiled. Prevention: always use the
  repository's project-Python launcher for Scriber tests.
- The attempted full installer rebuild was unnecessary for the owner's actual
  request and also reached an unrelated incomplete Windows-SDK installation.
  Scriber already separates its checksummed Python application layer from the
  frozen runtime, so the validated 136-file application layer was staged
  directly against the installed runtime cache key and exchanged with a
  rollback copy. The normal installed desktop/backend health check then passed.
  Prevention: for a compatible local-polishing code/model replacement, update
  the existing application layer and managed model cache first; build a full
  installer only when a distributable installer is explicitly required.
- The former local Gemma catalog artifacts were removed and the installed model
  cache now contains only the active `qad_q4_0` pointer and LFM installation.
  Scriber settings were migrated to enabled local post-processing with
  `qad_q4_0`; online API models remain available independently. A real hot
  completion using the installed llama.cpp b10158 runtime and managed public
  LFM model was accepted on `vulkan_compat` in 64.8 ms, and cleanup left zero
  `llama-server` processes. Prevention: verify replacement at three layers:
  one-model catalog, installed cache inventory, and one real warm completion.
- The standalone `--runtime-import-check` still reports the pre-existing frozen
  runtime's yt-dlp 2026.7.4 against the application check's 2026.8.19. This is
  unrelated to LFM: the normal installed desktop/backend starts and reports a
  healthy API. Prevention: do not turn an unrelated package-version diagnostic
  into a local-model blocker; resolve that runtime packaging drift in its own
  release task.
- The exact corpus used by the accepted winner is now public at dataset revision
  `7a9223ff99a0e4f5f89ac4992c7f876597716ec8`: 1,600 train, 200 validation,
  and 200 test rows. Anonymous round-trip downloads reproduced all three row
  counts and SHA-256 values. The dataset also publishes `TRAINING.md`,
  `training_recipe.json`, and the split audit. Prevention: publish the actual
  bound inputs and accepted recipe together; never reconstruct lineage from
  old run buckets or discarded corpora.
- Hugging Face storage was reduced to the active Parakeet STT model, the final
  LFM QAD model, and the exact LFM training dataset. The retired Gemma model,
  two obsolete datasets, and both historical run buckets were deleted. The two
  buckets alone held 41,044,031,350 bytes. Prevention: treat buckets as
  temporary execution storage and delete them after the accepted data, recipe,
  and model have been independently verified in their durable repositories.
- Super-squashing the LFM model history removed the superseded large-file blob
  but necessarily changed the immutable model revision to
  `d64f8a14a09b2916000d969edd18bc411745e53a`. The catalog, tests, documentation,
  installed application layer, and its checksum manifest were updated together.
  Prevention: never squash a shipping Hugging Face repository without
  atomically repinning every consumer and re-running anonymous artifact checks.
- A direct release-mode `cargo build` without `tauri/custom-protocol` compiled a
  desktop that still opened the development URL. Rebuilding with
  `--features tauri/custom-protocol` embedded the production webview and exposed
  only the LFM2.5 350M QAD-Q4_0 card. Prevention: a standalone Tauri production
  desktop build must enable the custom-protocol feature; backend health alone is
  not proof that the visible UI bundle is usable.
- The repinned installed product downloaded revision
  `d64f8a14a09b2916000d969edd18bc411745e53a` anonymously, promoted it to the
  active `qad_q4_0` pointer, and reproduced the expected 218,328,640-byte GGUF
  SHA-256. The visible settings showed only the LFM card, and the next real Live
  Mic session completed local post-processing. The superseded local revision,
  desktop/backend rollback copies, SDK package, verification downloads, and
  release build directory were then deleted. Prevention: accept a repin only
  after UI, pointer, bytes, hash, and one product path agree; remove rollback
  material only after those checks pass.
