# Scriber FLAC codec lab (Issue #18)

This directory is an isolated, reproducible candidate lab. It is deliberately
not part of the Tauri workspace, the audio sidecar, the Python backend, or the
installer. A successful result is codec evidence only; it must not be described
as production integration, provider compatibility, or an end-to-end latency
improvement.

## Candidate architecture

The lab consumes headerless mono PCM16-LE and encodes native FLAC with the
official `flacenc` 0.5.1 API. It accepts only the Issue #18 matrix:

- sample rates: 16 kHz and 48 kHz;
- durations: 5, 15, 30, and 60 seconds;
- channel count: one;
- sample width: 16 bits.

Two builds use the same source, encoder configuration, fixed 4096-sample block
size, and single-thread mode:

- `stable`: Rust 1.90.0 and flacenc's stable fake-SIMD compatibility path;
- `nightly-simd`: `nightly-2025-10-15` and flacenc's real upstream
  `simd-nightly` feature, which enables Rust `portable_simd`.

Both profiles also enable flacenc's `experimental` feature. This is not an
invented local SIMD gate: at the pinned upstream tag, `Weight::apply_simd` is
compiled only with `experimental`, and the upstream benchmark/test definitions
pair `experimental` with `simd-nightly`. The lab keeps that feature common to
Stable and Nightly so the comparison changes only the upstream SIMD route. The
experimental encoder options remain at their disabled defaults.

The nightly record says only that upstream portable SIMD was compiled. Exact
machine instructions remain compiler-selected; runtime CPU flags are recorded
as observations, not as proof of a particular instruction route.

Every invocation emits JSON containing the exact compiler commit, LLVM version,
build target, compile target features, flacenc build constants/features, runtime
CPU observations, input/output hashes and real measured encoder timings. All
production status fields remain `false`. The runner adds hashes of the binary,
Cargo manifest/lock, and toolchain manifest plus the pinned upstream tag and
commit.

## Validation

Run unit and Stable build tests:

```powershell
Set-Location native/scriber-codec-lab
python -m unittest discover -s tests -p "test_*.py"
cargo test --locked
cargo build --release --locked
```

Run the complete Stable matrix and independent byte-exact ffmpeg roundtrip:

```powershell
python scripts/run_matrix.py --profile stable --output-dir artifacts/stable
```

Nightly/SIMD is optional and uses the exact pin in `toolchains.json`:

```powershell
rustup toolchain install nightly-2025-10-15 --profile minimal
python scripts/run_matrix.py --profile nightly-simd --output-dir artifacts/nightly-simd
```

For comparative numbers, use the warmup-excluded ABBA/BAAB runner. It builds
both candidates first, then collects four temporally balanced samples for each
profile and every allowed sample-rate/duration series:

```powershell
python scripts/run_counterbalanced.py --output-dir artifacts/counterbalanced
```

### FFmpeg-fast and Rezin challengers

The second isolated control packet compares three whole-process WAV-to-FLAC
routes in a balanced Latin order, again using only the fixed duration/rate
matrix:

- the installed ffmpeg FLAC encoder with `-compression_level 0` as the fast
  control;
- `rezin-flac` 0.2.1 on pinned Stable Rust 1.97.0 with ordinary release
  codegen (`lto=false`, 16 codegen units);
- the identical Rezin source/toolchain with thin LTO and one codegen unit.

```powershell
python scripts/run_challenger_matrix.py --output-dir artifacts/challengers-final
```

Rezin 0.2.1 requires Rust 1.96 or newer and uses child processes based on
`available_parallelism()`. The published crate crashes on the required
5-second/16-kHz case when available parallelism (24 here) exceeds its 20 FLAC
frames. The lab therefore vendors exactly 0.2.1 and applies only a worker-count
bound. [The patch record](vendor/rezin-flac-0.2.1/SCRIBER_PATCH.md) contains the
crate archive, original source, patched source and upstream VCS hashes. The
complete upstream MIT license is retained. No production build references this
vendor directory.

The runner fails closed when the selected pinned toolchain or ffmpeg is absent,
when any input falls outside the matrix, or when ffmpeg's decoded PCM differs by
even one byte. Generated binaries and evidence live under ignored `target/` and
`artifacts/`; `Cargo.lock` remains the committed dependency lock.

## Local validation record (2026-07-20)

The pinned Windows x86-64 toolchains and ffmpeg 7.0 were available. Stable and
Nightly tests/builds passed. A warmup-excluded ABBA/BAAB run collected four
samples per profile and series. All 64 measured ffmpeg roundtrips were
byte-exact, and Stable/Nightly emitted identical FLAC bytes for every matching
fixture. The core-encode p50 observations were:

| Rate | Duration | Stable | Nightly SIMD | Nightly change |
| ---: | ---: | ---: | ---: | ---: |
| 16 kHz | 5 s | 3.26 ms | 3.37 ms | -3.18% |
| 16 kHz | 15 s | 10.41 ms | 9.46 ms | +9.05% |
| 16 kHz | 30 s | 18.68 ms | 17.70 ms | +5.28% |
| 16 kHz | 60 s | 33.58 ms | 48.95 ms | -45.77% |
| 48 kHz | 5 s | 10.82 ms | 10.15 ms | +6.21% |
| 48 kHz | 15 s | 31.07 ms | 25.62 ms | +17.53% |
| 48 kHz | 30 s | 49.16 ms | 53.37 ms | -8.56% |
| 48 kHz | 60 s | 107.48 ms | 118.32 ms | -10.08% |

Positive change means Nightly was faster; negative means it was slower. The
mixed result and millisecond-scale absolute deltas do not support a blanket
SIMD win or production promotion. Re-run the counterbalanced command on the
target release hardware before using these values for any later decision.

### Local challenger validation record (2026-07-20)

The three-profile runner collected three measured samples per profile/series;
its warmups and builds were excluded. All 72 independent ffmpeg decodes were
byte-exact. Values below are whole-process p50 timings, so they include WAV
parsing, encoder process/worker startup, encoding, and FLAC output writes.
Positive LTO change means the LTO Rezin binary was faster than ordinary Rezin.

| Rate | Duration | FFmpeg fast | Rezin default | Rezin LTO | LTO change |
| ---: | ---: | ---: | ---: | ---: | ---: |
| 16 kHz | 5 s | 87.75 ms | 379.63 ms | 409.02 ms | -7.74% |
| 16 kHz | 15 s | 98.15 ms | 461.34 ms | 445.58 ms | +3.42% |
| 16 kHz | 30 s | 105.39 ms | 756.51 ms | 705.44 ms | +6.75% |
| 16 kHz | 60 s | 150.33 ms | 839.39 ms | 864.65 ms | -3.01% |
| 48 kHz | 5 s | 97.64 ms | 530.71 ms | 443.91 ms | +16.36% |
| 48 kHz | 15 s | 95.01 ms | 810.58 ms | 694.26 ms | +14.35% |
| 48 kHz | 30 s | 98.84 ms | 761.90 ms | 973.99 ms | -27.84% |
| 48 kHz | 60 s | 113.68 ms | 955.57 ms | 892.42 ms | +6.61% |

Rezin was slower than FFmpeg-fast in every measured series. LTO was mixed and
therefore is not a promotable optimization. Rezin produced slightly smaller
files for the two shortest 16-kHz fixtures, but larger files for the other six;
the default/LTO builds emitted identical Rezin FLAC bytes.

PGO remained fail-closed: the pinned 1.97.0 toolchain does not have its
`llvm-tools-preview` component or `llvm-profdata`. The runner records this as
`fail_closed_not_run` and does not download an implicit tool or invent PGO
numbers. A future PGO packet must explicitly pin that component, training
corpus, merged profile hash, and profile-use flags before it may be measured.

## Promotion boundary

This lab does not answer whether a provider accepts FLAC for an exact endpoint,
whether encoding can remain nonblocking during live capture, or whether an
installed activation-to-visible-text KPI improves. Production promotion still
requires those route-specific contracts and matched installed evidence. The
lab must stay independent of the normal Scriber build until that evidence
exists.
