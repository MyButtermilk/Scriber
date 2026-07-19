# Installer-size AutoResearch contract

This profile runs one deterministic installer-size experiment at a time. The
Codex Goal controller owns hypothesis selection and product edits; the checked-in
harness owns run isolation, deadlines, immutable evidence, doctor gates, and
packet dispatch. The harness must never spawn an autonomous LLM loop.

## Fixed campaign rules

- Research duration is exactly 43,200 seconds. Preflight and one fully gated
  baseline build are outside that budget. `researchStartedAtUtc` and the fixed
  deadline are written only after that baseline inventory and the run-phase
  doctor pass. This does not change the two fresh final inventories required
  for promotion evidence.
- The installer remains self-contained. No feature, provider, format, language,
  sidecar, export, or first-use runtime may be removed without a functionally
  equivalent bundled replacement.
- The reference installer uses explicit NSIS `bzip2` compression. Historical
  release artifacts are context only and never an active baseline.
- Each packet contains one falsifiable hypothesis and dispatches one fixed,
  checked-in measurement action. Product changes are prepared before dispatch.
- Every immutable packet binds `lane`, `sourceCommit`, timeout, expected byte
  reduction, and result path; candidate packets additionally bind the parent
  champion and `comparisonKind`. A payload candidate also binds
  `parentSourceTreeOid`, and the candidate commit must be a non-merge commit
  whose immediate parent tree is exactly the current baseline/champion tree.
  This prevents a later keep from silently stacking a previously discarded
  source change. The only
  production dispatcher is `scripts/run_installer_size_packet.ps1`; `next`
  executes exactly the existing packet and never formulates or loops an LLM.
- Session initialization atomically snapshots both baseline requirements files.
  Baseline verification always uses those immutable bytes; a committed payload
  experiment may remove current requirements, but its source-specific
  environment must install exclusively from the frozen run wheelhouse.
- The pinned toolchain manifest covers the complete plain, non-reparse
  `Frontend/node_modules` tree and separately hashes the native Windows Tauri
  CLI, package lock, Node archive, the complete Tauri NSIS tree, and every Rust
  tool used by final gates. Research builds cannot install PyInstaller or any
  other package from the network; the frozen environment is re-attested after
  every build.
- Installer bytes must fall by at least `max(262144, champion * 0.0025)` and the
  installed payload must not grow. Installation p50 and p95 may regress by at
  most five percent. The final 50-Mbit/s download-plus-install p50 must improve
  by at least `max(0.5 seconds, one percent)`.
- QuickJS may replace Deno only after feature, performance, security,
  provenance, parallel-job, cleanup, and rollback gates pass. Exactly one
  JavaScript runtime is bundled.
- The loop performs no push, merge, tag, upload, signing, publication, cache
  publication, or updater release.

## Adaptive hypothesis policy

Each lane starts with a Beta(1,1) prior. Valid keeps/discards update its
posterior, every packet updates a duration EWMA with alpha 0.5, and the ledger
retains expected versus actual installer reduction plus bounded reason codes.
Three valid discards lock a lane. Ten consecutive valid discards without a keep
enter plateau finalization. Every fourth packet may explore one previously
untested lane only when its expected potential is at least 1 MiB. The effective
finalization reserve is `max(5400 seconds, 1.25 * maximum lane duration EWMA)`.

Payload experiments retain explicit bzip2. A `compression` experiment may
repackage only the byte-attested current champion payload as bzip2, zlib, or
LZMA without rebuilding it. Semantic tree identity, all functional gates, 20
counterbalanced installation pairs, and the combined 50-Mbit/s metric remain
mandatory; this never changes the official release compression implicitly.

## State and evidence

All state is namespaced under
`autoresearch-results/installer-size/<canonical-rfc4122-run-id>/`. The run
manifest and input snapshots are immutable. Packet results use
`InstallerResearchResultV1`; the append-only ledger is sequence- and
SHA-256-chained. One run-scoped cross-process mutation lock serializes resume,
abandon, and packet dispatch; the Windows producer runs in a kill-on-close Job
Object so an interrupted controller cannot leave a writer running. A pending
packet that no longer fits may be abandoned only through its immutable,
allowlisted tombstone transition. A crash never extends the original deadline.

The final preview distinguishes research completion, research-champion
readiness, and release readiness. An unsigned research result or one without
provider secrets can be a research champion, but `releaseReady` remains false
until the normal signed tag-release workflow supplies its independent gates.
Research-champion readiness additionally requires two fresh final inventories
from distinct build roots and a final 20-pair baseline/champion timing report.
Even complete final evidence does not set `researchComplete` before the exact
43,200-second manifest deadline.
The exact integer combined p50 improvement must be at least
`max(500,000,000 ns, 1% of baseline)`; install p50 and p95 remain within 5%.
Final packets bind the current clean commit and the kept champion commit to one
exact `championSourceTreeOid`, so a scoped revert commit is valid only when its
Git tree is byte-identical to the champion. Both replicas require all ten
packet-local functional gates with retained, rehashed JSON artifacts. Replica 1
also requires retained evidence for the complete Python, pinned Frontend, and
pinned Rust test/typecheck/build/fmt/clippy suite. A hash without its fixed
run-local evidence file is never promotion authority.
