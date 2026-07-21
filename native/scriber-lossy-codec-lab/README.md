# Scriber lossy codec lab (Issue #18)

This directory is an isolated, offline, non-production research and release-
control lab. It is not a member of the Tauri workspace, is not imported by the
Python backend, and is not packaged in an installer. Passing evidence here is
not production integration, provider compatibility, or an installed
activation-to-visible-text speedup.

The lab accepts only headerless mono signed PCM16-LE with the fixed Issue #18
matrix:

- source sample rates: 16 kHz and 48 kHz;
- durations: 5, 15, 30, and 60 seconds;
- one channel and 16-bit samples;
- 64 kbit/s output for every candidate and control.

The Python runners never make a network request. Cargo is invoked with
`--locked --offline`; the locked crates therefore have to be present in the
normal build cache before an evidence run starts. Provider keys are neither
read nor accepted.

## Candidates and controls

### MP3

`mp3_lame_in_process_v1` uses `mp3lame-encoder` 0.2.4 over the locked
`mp3lame-sys` 0.1.11 bundle of LAME 3.100. The lab consumes PCM directly,
encodes CBR 64 kbit/s mono MP3, preserves the current Scriber output rate of
16 kHz, flushes the stream, and emits LAME delay/padding metadata. A 48-kHz
fixture uses LAME's internal 48-to-16-kHz resampler.

`mp3_ffmpeg_libmp3lame_current_control_v1` is the existing Scriber command
shape: raw PCM on stdin, MP3 on stdout, `libmp3lame`, 64 kbit/s, 16 kHz, mono.
The command contract is deliberately kept equivalent to
`mp3_encode_pcm_pipe_args` rather than inventing a faster FFmpeg profile.

The requested Shine-RS/Shine challenger is fail-closed. There is no reviewed,
locked, MSVC-reproducible in-process Shine build in this lab. Some FFmpeg
builds advertise external `libshine`, but that neither creates an in-process
Rust challenger nor proves support for the fixed 16-kHz output contract. Every
matrix writes `shine-challenger-status.json` with the encoder advertisement,
tool hash, reasons, and all promotion flags false. No Shine timing is silently
substituted.

### Ogg Opus

`opus_ruopus_ogg_v1` uses pure-Rust `ruopus` 0.1.2 with default features off
and only `std` on. Ruopus performs stable-runtime x86 dispatch to AVX2+FMA when
eligible and otherwise SSE2; the evidence records CPU flags as route
eligibility, not as proof that every hot loop executed a particular
instruction. It emits a complete RFC 7845 Ogg Opus stream with `OpusHead`,
`OpusTags`, packet granule positions, and EOS.

Ruopus's public encoder consumes 48-kHz floating-point frames. A 16-kHz PCM16
fixture is therefore converted by the measured candidate using a deterministic
linear 3x interpolation; a 48-kHz fixture uses exact `i16 / 32768` scaling.
That preparation route and its separate duration are attested. The encoder
uses mono, 64 kbit/s, and 20-ms packets.

`opus_ffmpeg_libopus_reference_control_v1` is the permitted reference control:
FFmpeg's `libopus` encoder plus Ogg Opus muxer, mono, 64 kbit/s, VoIP
application, 20-ms packets, and 48-kHz output. It intentionally does not claim
to use `libopusenc`; choosing the already identifiable `libopus` reference
avoids introducing an unpinned native `libopusenc` build.

## Independent validation and quality

Each artifact is probed and independently decoded by a second FFmpeg process.
The gate verifies codec, container, mono channel count, encoded sample rate,
packet/frame count, stream duration, decoded duration, alignment, and bounded
head/tail behavior. Ogg/Opus must be a real Ogg container, not raw Opus
packets.

Lossy output is never compared for byte equality. The runner aligns decoded
PCM to the deterministic fixture, then records:

- raw SNR;
- scale-invariant SNR after a least-squares gain fit;
- normalized and alignment correlation;
- gain and RMS ratio;
- alignment lag, decoded tail, uncovered source tail, and duration error.

The structural research gate is intentionally conservative: correlation must
be at least 0.70, scale-invariant SNR at least 3 dB, absolute alignment at most
120 ms, and decoded duration/tail error at most 200 ms. These are corruption
guards, not a product-quality promotion threshold.

Every record includes hashes for input, encoded and decoded artifacts, the
candidate binary, FFmpeg, FFprobe, Cargo files, toolchain files, and the lab
source tree. It also includes the complete Cargo lock package/checksum set,
Rust compiler commit/LLVM/target features, FFmpeg configuration, crate feature
selection, CPU vendor/brand/feature eligibility, iteration, and exact encoder
configuration. Absolute executable paths are deliberately omitted from JSON.

## Running the controls

Build and test without network access:

```powershell
Set-Location native/scriber-lossy-codec-lab
cargo test --locked --offline
cargo clippy --all-targets --locked --offline -- -D warnings
python -m unittest discover -s tests -p "test_*.py"
```

Run one full control matrix (one measured result per candidate/rate/duration):

```powershell
python scripts/run_matrix.py --candidate all --output-dir artifacts/matrix
```

Run the comparative evidence packet:

```powershell
python scripts/run_counterbalanced.py --output-dir artifacts/counterbalanced
```

The comparative runner builds once, performs one excluded 5-second warmup for
each candidate, then runs each codec pair in ABBA/BAAB order. Matrix traversal
alternates forward and reverse. This yields four measured samples per
candidate, source-rate, and duration series. Reported candidate/control wall
times use the same separate-process lab boundary; Rust's inner
codec-and-container time is also retained but is not compared directly to the
FFmpeg process wall. Generated `target/` and `artifacts/` directories are
ignored.

## Local decision record (2026-07-20)

The pinned Windows x86-64 toolchain, FFmpeg 7.0, and the fixed matrix completed
with 128/128 measured validations passing after four excluded 5-second
warmups. Each row below is the median of four warmup-excluded ABBA/BAAB samples.
`Change` is `(control - candidate) / control`; positive values favor the Rust
candidate. These are separate-process lab wall times, not installed or provider
latency.

| MP3 source | Duration | LAME candidate | FFmpeg control | Saved | Change | LAME codec+container |
| ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 16 kHz | 5 s | 50.95 ms | 87.43 ms | 36.47 ms | +41.72% | 7.43 ms |
| 16 kHz | 15 s | 76.74 ms | 101.04 ms | 24.30 ms | +24.05% | 26.50 ms |
| 16 kHz | 30 s | 89.92 ms | 138.01 ms | 48.09 ms | +34.85% | 38.10 ms |
| 16 kHz | 60 s | 154.29 ms | 201.17 ms | 46.89 ms | +23.31% | 85.08 ms |
| 48 kHz | 5 s | 63.82 ms | 97.06 ms | 33.23 ms | +34.24% | 14.00 ms |
| 48 kHz | 15 s | 81.65 ms | 104.61 ms | 22.96 ms | +21.95% | 35.29 ms |
| 48 kHz | 30 s | 112.05 ms | 159.72 ms | 47.67 ms | +29.85% | 61.84 ms |
| 48 kHz | 60 s | 173.93 ms | 206.28 ms | 32.35 ms | +15.68% | 118.35 ms |

The corrected LAME/Xing metadata frame replaces the reserved first MPEG frame
instead of adding a frame. Independent FFmpeg decoding then produced the exact
fixture duration and zero decoded tail in all eight candidate series. Minimum
scale-invariant SNR was 45.00--45.63 dB at 16 kHz and 41.52--42.86 dB for
48-kHz sources, close to the matched control's 45.06--45.72 dB and
42.74--42.89 dB. Decision: retain LAME as the only promising lossy candidate
for a later provider/capture-time integration experiment; this lab does not
promote it into Scriber.

| Opus source | Duration | Ruopus candidate | libopus control | Candidate minus control | Change |
| ---: | ---: | ---: | ---: | ---: | ---: |
| 16 kHz | 5 s | 631.94 ms | 137.20 ms | +494.74 ms | -360.61% |
| 16 kHz | 15 s | 2207.43 ms | 151.17 ms | +2056.25 ms | -1360.20% |
| 16 kHz | 30 s | 3667.73 ms | 185.38 ms | +3482.35 ms | -1878.49% |
| 16 kHz | 60 s | 6576.34 ms | 331.49 ms | +6244.84 ms | -1883.85% |
| 48 kHz | 5 s | 578.47 ms | 101.00 ms | +477.48 ms | -472.76% |
| 48 kHz | 15 s | 2205.63 ms | 114.81 ms | +2090.82 ms | -1821.08% |
| 48 kHz | 30 s | 3782.84 ms | 182.08 ms | +3600.76 ms | -1977.55% |
| 48 kHz | 60 s | 6694.77 ms | 381.99 ms | +6312.78 ms | -1652.60% |

Ruopus produced valid Ogg/Opus with 27.29--27.54 dB minimum scale-invariant
SNR and only the attested 2.5-ms Opus pre-skip difference, but its measured wall
time was about 4.6x--20.8x the reference control. Decision: do not promote this
Ruopus release for the latency path. Shine remains fail-closed and unmeasured.

## License boundary

- This lab's original code is MIT.
- Ruopus 0.1.2 is MIT.
- `mp3lame-encoder`, `mp3lame-sys`, and bundled LAME are LGPL-3.0. The lab's
  static link is research-only. Shipping it would require a dedicated LGPL
  source, notice, and relinking-compliance design and review.
- FFmpeg is an external control executable. Its exact version, hash, and build
  configuration are recorded; the locally observed full build is GPL-enabled.
- No Shine source is included. A future Shine pin needs its own source,
  Windows-build, patent, and redistribution review.

This is a concise dependency inventory, not legal advice.

## Promotion boundary

All evidence has `productionReady=false`, `productionIntegrated=false`, and
`productionPromoted=false`. Promotion would still require an exact provider /
model / endpoint format contract, a bounded nonblocking capture-time worker,
failure and cancellation ownership, installer/release license compliance, and
matched installed 5/15/30/60-second activation-to-exact-visible-text evidence.
The lab must remain separate from normal builds until those gates are met.
