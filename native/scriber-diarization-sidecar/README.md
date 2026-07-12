# Scriber diarization sidecar

This crate builds the optional, isolated local speaker-diarization worker. It
is intentionally outside `Frontend/src-tauri` and is not linked into either the
Tauri shell or `scriber-audio-sidecar`.

## Build

The crate pins both `sherpa-onnx` and its native `sherpa-onnx-sys` ABI to
**1.13.3** with their `static` features. The explicit second pin prevents the
safe wrapper's transitive caret requirement from selecting a newer native
archive. On Windows x64 the upstream build script expects
`sherpa-onnx-v1.13.3-win-x64-static-MT-Release-lib.tar.bz2`. Release builds
should place a separately SHA-256-verified copy of that archive in a controlled
directory and set `SHERPA_ONNX_ARCHIVE_DIR` to that directory before building.
Run Cargo from this crate directory so `.cargo/config.toml` also enables the
static MSVC CRT that matches Sherpa's `static-MT` archive:

```powershell
Push-Location native/scriber-diarization-sidecar
cargo build --release --locked
Pop-Location
```

`build.rs` rejects a Windows release build without `crt-static`, preventing an
accidental invocation from silently producing a differently linked artifact.

The worker executable is a versioned backend resource of the signed Scriber
installer/updater. Release preparation stages it at
`Frontend/src-tauri/target/release/backend/tools/diarization/` together with the
build-generated `scriber-diarization-sidecar.manifest.json`; Tauri maps that
tree to the installed backend directory. Only the models and their license
notices remain an explicit post-install component.
The worker stays a separate process; bundling does not link it into Tauri or
the live-audio sidecar.

`scripts/build_tauri_backend_sidecar.ps1 -BundleRustDiarizationSidecar`
generates the attestation automatically. For a focused local diagnostic, the
same writer can be invoked directly:

```powershell
python scripts/write_diarization_worker_manifest.py `
  --executable <backend-resource>\scriber-diarization-sidecar.exe `
  --output <backend-resource>\scriber-diarization-sidecar.manifest.json
```

The frozen Python runtime accepts only that allowlisted resource location and
fails closed when the manifest or digest is missing. Source checkouts may use
this crate's `target/release` and `target/debug` outputs for development.

## Process contract

`--version` and `--self-test` return bounded JSON without loading models or user
audio. `--stdio` (or no argument) consumes one JSON object from one stdin line,
emits one JSON response line, and exits. The schema is version 1:

```json
{
  "schemaVersion": 1,
  "jobId": "opaque-id",
  "audioPath": "C:/validated-job/audio.wav",
  "segmentationModelPath": "C:/validated-component/segmentation.onnx",
  "embeddingModelPath": "C:/validated-component/embedding.onnx",
  "clustering": { "numSpeakers": null, "threshold": 0.9 },
  "limits": { "maxDurationMs": 7200000, "maxResidentBytes": 1073741824 }
}
```

The parent must set both roots for every worker process:

- `SCRIBER_DIARIZATION_JOB_ROOT`: canonical root containing the normalized WAV;
- `SCRIBER_DIARIZATION_COMPONENT_ROOT`: canonical root containing both models.

The worker canonicalizes all three files again and rejects escapes, relative
paths, symlink escapes, non-regular files, wrong extensions, oversized files,
and WAV input other than mono 16-bit PCM at 16 kHz. On Windows it applies the
requested process-memory ceiling with a Job Object before loading audio or
models. The two-hour, 1-GiB ceilings are hard protocol maxima; the parent still
owns the wall-clock timeout and termination policy.

Success returns model/engine versions, duration, speaker count, and sorted
millisecond turns. Native cluster labels are deterministically renumbered by
first chronological appearance. Errors contain only fixed codes and fixed
messages: no file paths, audio, transcript text, or native error strings. In
normal Windows worker mode the C-runtime stderr descriptor is redirected to
`NUL` before request parsing so an unexpected native-library diagnostic cannot
leak a model or audio path around the JSON error contract.
