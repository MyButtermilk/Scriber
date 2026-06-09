# FFmpeg Footprint Strategy

Last updated: 2026-06-09

This document records the repository audit, capability matrix, recommended
FFmpeg profile, build strategy, and validation plan for reducing Scriber's
bundled FFmpeg footprint without losing real media functionality.

## Decision Summary

- Keep `ffmpeg.exe` and `ffprobe.exe` in the standard Windows installer.
- Do not ship `ffplay.exe`.
- Keep MP3 encoding through `libmp3lame` for Azure MAI upload preparation
  because upload latency is the user priority.
- Keep WebM/Opus encoding through `libopus` for general file/video/YouTube
  normalization.
- Do not persist WAV/PCM as an upload artifact. PCM remains allowed only as a
  process pipe for the Pipecat file-input transport.
- Do not enable FFmpeg network protocols in the minimal production profile.
  Website extraction belongs to `yt-dlp`; FFmpeg handles only local files plus
  local stdin/stdout pipes for live PCM-to-MP3 preparation.
- Avoid GPL/nonfree libraries unless later explicitly approved. `libmp3lame`
  and `libopus` are acceptable candidates for an LGPL-oriented build, but final
  legal review must verify the exact build configuration and notices.

The current standard local reference build is very large:

| Tool | Local reference size |
| --- | ---: |
| `ffmpeg.exe` | 133.58 MiB |
| `ffprobe.exe` | 133.43 MiB |
| Total media tools | 267.01 MiB |

Status 2026-06-09: A real MSYS2/UCRT64 Profile-B custom build has been
compiled and validated. The portable media-tool folder contains `ffmpeg.exe`
`2.55 MiB`, `ffprobe.exe` `2.43 MiB`, plus `libmp3lame-0.dll` `0.31 MiB`,
`libopus-0.dll` `0.47 MiB`, and `libwinpthread-1.dll` `0.06 MiB`; total
validated media-tool size is `4.98 MiB`. The build passed the Profile-B
manifest gate, `26/26` fixture checks, `5/5` media-preparation checks, the
sidecar slim-media validation/copy gate, a full NSIS installed frontend/media
smoke, and real installed file plus YouTube transcription workflows with
completed summaries. The GitHub Windows release workflow now builds Profile B
through MSYS2 and passes it as `MediaToolsDir` to the installer build. Gyan
Essentials remains the conservative local/direct-Tauri fallback path, not the
target release footprint.

The local reference build reports `--enable-gpl`, `--enable-libmp3lame`, and
`--enable-libopus`, with no `--enable-nonfree`. A production custom build should
remove GPL-only features unless a future workflow proves they are required.

## Repository Audit

| Area | File | FFmpeg/ffprobe/yt-dlp usage | Local vs remote |
| --- | --- | --- | --- |
| Upload compression and video extraction | `src/web_api.py` | Builds WebM/Opus output from uploaded audio/video; probes duration with ffprobe. | Local files only. |
| YouTube/media-site download | `src/youtube_download.py` | `yt-dlp` downloads website content; ffprobe checks whether WebM contains video; ffmpeg normalizes downloaded local files to audio-only WebM/Opus. | Remote URL goes to `yt-dlp`; FFmpeg receives local files only. |
| File pipeline transport | `src/audio_file_input.py` | ffmpeg decodes a local file to raw PCM on stdout for Pipecat. | Local files only; no persisted WAV. |
| Azure MAI preparation | `src/azure_mai_stt.py` | all non-MP3 file inputs are transcoded to MP3 64k mono 16 kHz; live PCM buffers are encoded to MP3 before upload. | Local files only or stdin pipe. |
| Runtime tool lookup | `src/runtime/media_tools.py` | Resolves explicit env path, `SCRIBER_MEDIA_TOOLS_DIR`, bundled app dirs, then PATH. | Production should resolve bundled tools before PATH. |
| Shared command builders | `src/runtime/ffmpeg_commands.py` | Centralizes ffmpeg/ffprobe argument arrays and rejects remote URLs. | Local paths only. |
| Media smoke gate | `scripts/smoke_media_preparation.py` | Exercises upload compression, video extraction, YouTube post-download normalization, Azure MAI MP3 preparation, and ffprobe duration. | Synthetic local fixtures. |
| Sidecar packaging | `scripts/build_tauri_backend_sidecar.ps1` | Copies ffmpeg/ffprobe into `tools\ffmpeg`; validates slim capabilities. | Packaged resource path. |
| Windows release build | `scripts/build_windows.ps1` | Forwards media-tool dir and slim-validation flags; can run media smokes. | Standard release bundles tools. |
| Installer smoke | `scripts/smoke_windows_installer.ps1` | Validates installed `backend\tools\ffmpeg` or resource fallback. | Installed package. |

No current runtime path intentionally passes a website URL directly to FFmpeg.
The new shared command builders reject URL-like inputs, making that boundary
test-covered.

## Exact Runtime Commands

The app now constructs FFmpeg commands through `src/runtime/ffmpeg_commands.py`.
The command shapes are:

### WebM/Opus normalization

```powershell
ffmpeg -hide_banner -loglevel error -nostdin -y -i <local-input> -vn -map 0:a:0 -c:a libopus -b:a <bitrate> -ar 16000 -ac 1 <output.webm>
```

Used by:

- `src/web_api.py` for upload compression and video audio extraction.
- `src/youtube_download.py` for local files downloaded by `yt-dlp`.

### Azure MAI MP3 preparation

```powershell
ffmpeg -hide_banner -loglevel error -nostdin -y -i <local-input> -vn -map 0:a:0 -c:a libmp3lame -b:a 64k -ar 16000 -ac 1 <output.mp3>
```

Used by `src/azure_mai_stt.py` for every non-MP3 file input before Azure MAI
upload. Existing `.mp3` files are uploaded directly. Live PCM buffers are
encoded to MP3 through an FFmpeg pipe and uploaded as `audio/mpeg`, not WAV.

### Azure MAI live PCM to MP3 pipe

```powershell
ffmpeg -hide_banner -loglevel error -f s16le -ar <input-sample-rate> -ac <input-channels> -i pipe:0 -vn -map 0:a:0 -c:a libmp3lame -b:a 64k -ar 16000 -ac 1 -f mp3 pipe:1
```

Used by `src/azure_mai_stt.py` for live buffered Azure MAI audio. This path
requires the local `pipe` protocol and `s16le` demuxer in addition to
`libmp3lame`; it avoids a large WAV upload.

Rationale: MP3 64k keeps upload size small. In a local 20-second speech test,
MP3 64k produced about 157 KB, while FLAC produced about 330 KB. Encoding time
was below 100 ms for both; upload size dominates latency on slower connections.

### PCM pipe for Pipecat file input

```powershell
ffmpeg -hide_banner -loglevel error -nostdin -i <local-input> -vn -map 0:a:0 -ac 1 -ar 16000 -f s16le -acodec pcm_s16le -
```

Used by `src/audio_file_input.py`. This emits raw PCM to stdout only and does
not create a stored WAV upload artifact. This path requires the raw
`s16le`/`pcm_s16le` output muxer in addition to the `pcm_s16le` encoder.

### Duration probing

```powershell
ffprobe -v error -show_entries format=duration -of default=noprint_wrappers=1:nokey=1 <local-input>
```

Used by `src/web_api.py` for best-effort duration metadata.

### WebM video-stream probing

```powershell
ffprobe -v error -select_streams v:0 -show_entries stream=codec_type -of default=noprint_wrappers=1:nokey=1 <local-input>
```

Used by `src/youtube_download.py` to avoid returning a WebM file that still
contains video.

## ffprobe Decision

`ffprobe` is not needed for every transcode, but it remains part of the standard
release profile because:

- it detects whether a downloaded WebM has a video stream,
- it provides reliable duration metadata,
- release readiness currently requires `ffprobe_duration_probe`,
- removing it saves substantial space only if the product accepts weaker media
  diagnostics and weaker YouTube/WebM post-download certainty.

`-SkipBundledFfprobe` remains an explicit size experiment only, not the standard
release path.

## yt-dlp Boundary

`yt-dlp` is the website extractor. FFmpeg is not the website extractor.

Current behavior:

- `src/youtube_download.py` sends remote URLs only to `yt-dlp`.
- The app first requests audio-only formats:
  `bestaudio[ext=webm]/bestaudio[ext=m4a]/bestaudio`.
- It falls back to broader local-file outputs only when strict selectors fail.
- Any returned local file is checked/normalized to audio-only WebM/Opus.
- Shared FFmpeg command builders reject URL-like inputs.

If future workflows require `yt-dlp` merge/postprocessing, pass the bundled
FFmpeg location explicitly to `yt-dlp` instead of enabling FFmpeg network
protocols.

## Capability Matrix

| Use case | Example input | Demuxer | Decoder | Parser | Filter need | Output | Encoder | ffprobe | yt-dlp | Priority |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| MP3 CBR to WebM/Opus | `.mp3` | `mp3` | `mp3` | `mpegaudio` | resample/downmix | `webm` | `libopus` | no | no | required |
| MP3 VBR to WebM/Opus | `.mp3` | `mp3` | `mp3` | `mpegaudio` | resample/downmix | `webm` | `libopus` | no | no | required |
| WAV PCM 16-bit input | `.wav` | `wav` | `pcm_s16le` | none | resample/downmix | `webm` | `libopus` | optional | no | required input |
| WAV PCM 24-bit input | `.wav` | `wav` | `pcm_s24le` | none | resample/downmix | `webm` | `libopus` | optional | no | required input |
| WAV float input | `.wav` | `wav` | `pcm_f32le` | none | resample/downmix | `webm` | `libopus` | optional | no | required input |
| MOV AAC audio | `.mov` | `mov` | `aac` | `aac` | resample/downmix | `webm` | `libopus` | optional | no | required |
| MOV/M4A ALAC audio | `.mov`, `.m4a` | `mov` | `alac` | none | resample/downmix | `webm` or `mp3` | `libopus`/`libmp3lame` | optional | no | required if feasible |
| MP4/M4A AAC audio | `.mp4`, `.m4a` | `mov` | `aac` | `aac` | resample/downmix | `webm` | `libopus` | optional | no | required |
| WebM/Opus to pipe PCM | `.webm` | `matroska` | `opus` | `opus` | resample/downmix | stdout `s16le` | `pcm_s16le` | no | no | required internal |
| WebM/Opus to WebM/Opus | `.webm` | `matroska` | `opus` | `opus` | resample/downmix | `webm` | `libopus` | video check | maybe | required |
| MKV/WebM video extraction | `.mkv`, `.webm` | `matroska` | common audio | codec-specific | resample/downmix | `webm` | `libopus` | recommended | maybe | required |
| yt-dlp YouTube M4A | `.m4a` | `mov` | `aac` | `aac` | resample/downmix | `webm` | `libopus` | optional | yes | required |
| yt-dlp YouTube WebM/Opus | `.webm` | `matroska` | `opus` | `opus` | optional | `webm` | `libopus` if video present/non-audio-only | video check | yes | required |
| yt-dlp merged MP4 | `.mp4` | `mov` | `aac` and/or other audio | codec-specific | resample/downmix | `webm` | `libopus` | optional | yes | required |
| Azure MAI non-MP3 source | `.wav`, `.flac`, `.webm`, `.m4a`, `.mp4` | source-specific | source-specific | source-specific | resample/downmix | `mp3` | `libmp3lame` | no | maybe | required for latency |
| Azure MAI live PCM buffer | stdin `s16le` | `s16le` | `pcm_s16le` | none | resample/downmix | stdout `mp3` | `libmp3lame` | no | no | required for latency |
| No-audio video | `.mp4` | source-specific | none | n/a | n/a | fail | n/a | helpful | maybe | required error |
| Corrupted input | any | source-specific | source-specific | source-specific | n/a | fail | n/a | helpful | no | required error |
| Unsupported codec | any | source-specific | missing decoder | source-specific | n/a | fail | n/a | helpful | no | required error |

## Recommended Profiles

### Profile A: Smallest Practical Local-Media Build

Purpose: local file processing only, no direct remote URL support.

Required:

- programs: `ffmpeg`, `ffprobe`
- protocols: `file`, `pipe`
- demuxers: `mp3`, `wav`, `mov`, `matroska`, `ogg`, `flac`, `s16le`
- muxers: `webm`, `mp3`, `s16le`
- decoders: `mp3`, `aac`, `opus`, `vorbis`, `flac`, `alac`,
  `pcm_s16le`, `pcm_s24le`, `pcm_s32le`, `pcm_f32le`, `pcm_u8`
- encoders: `libopus`, `libmp3lame`, `pcm_s16le` for stdout/raw support where
  FFmpeg requires encoder registration
- raw `s16le` output muxing for PCM stdout pipes
- parsers: `mpegaudio`, `aac`, `opus`, `vorbis`, `flac`
- filters: `aresample`, `aformat`, `anull`, `pan`

Exclude:

- `ffplay`
- network protocols
- video encoders (`x264`, `x265`, VP8/VP9/AV1 encoders)
- hardware acceleration stacks
- DVD/Blu-ray/capture devices/subtitles/OCR/VMAF
- GPL/nonfree components unless separately approved

### Profile B: yt-dlp Post-Processing Build

Purpose: standard production recommendation.

Profile B equals Profile A plus practical support for local files produced by
`yt-dlp`, including M4A, MP4, WebM/Opus and merged MP4/WebM files.

Add only if fixture tests prove needed:

- additional parsers for stream discovery in video containers, such as
  `h264`/`hevc`, without enabling video decoding or encoding
- demuxers such as `mpegts` or `concat` if real downloaded/merged files need
  them in local post-processing

Do not add FFmpeg network protocols for this profile.

### Profile C: Compatibility Fallback Build

Purpose: beta/diagnostic fallback.

- Use Gyan release essentials or another broad LGPL-compatible build.
- Avoid GPL/nonfree unless explicitly approved.
- Keep behind explicit build input or diagnostic fallback, not as the target
  production footprint.

## Candidate Configure Lines

These are candidates and must be validated with real builds plus the fixture
matrix before becoming release truth.

### Profile A Candidate

```bash
./configure \
  --enable-small \
  --disable-everything \
  --disable-autodetect \
  --disable-debug \
  --disable-doc \
  --disable-network \
  --disable-ffplay \
  --enable-protocol=file \
  --enable-protocol=pipe \
  --enable-demuxer=mp3 \
  --enable-demuxer=wav \
  --enable-demuxer=mov \
  --enable-demuxer=matroska \
  --enable-demuxer=ogg \
  --enable-demuxer=flac \
  --enable-demuxer=pcm_s16le \
  --enable-muxer=webm \
  --enable-muxer=mp3 \
  --enable-muxer=pcm_s16le \
  --enable-decoder=mp3 \
  --enable-decoder=aac \
  --enable-decoder=opus \
  --enable-decoder=vorbis \
  --enable-decoder=flac \
  --enable-decoder=alac \
  --enable-decoder=pcm_s16le \
  --enable-decoder=pcm_s24le \
  --enable-decoder=pcm_s32le \
  --enable-decoder=pcm_f32le \
  --enable-decoder=pcm_u8 \
  --enable-libopus \
  --enable-encoder=libopus \
  --enable-libmp3lame \
  --enable-encoder=libmp3lame \
  --enable-encoder=pcm_s16le \
  --enable-parser=mpegaudio \
  --enable-parser=aac \
  --enable-parser=opus \
  --enable-parser=vorbis \
  --enable-parser=flac \
  --enable-filter=aresample \
  --enable-filter=aformat \
  --enable-filter=anull \
  --enable-filter=pan
```

### Profile B Candidate

Start with Profile A, then add only if fixtures prove the need:

```bash
  --enable-parser=h264 \
  --enable-parser=hevc \
  --enable-demuxer=mpegts \
  --enable-demuxer=concat
```

Do not add `--enable-protocol=http`, `--enable-protocol=https`, TLS, or TCP
unless the architecture changes and FFmpeg intentionally receives remote URLs.

## Build-System Strategy

Recommended implementation order:

1. Use Profile B as the CI/release media-tool input. Keep Gyan Essentials as
   the conservative local/direct-Tauri fallback and rollback path.
2. Generate the Profile B build kit with
   `python scripts/ffmpeg/create_profile_b_build_kit.py --output-dir
   build/ffmpeg-profile-b`. The helper writes:
   - `configure-profile-b.args`,
   - `configure-profile-b.sh`,
   - `profile-b-build-plan.json` with the source URL/ref and post-build
     validator, fixture-smoke, media-smoke, and sidecar-gate commands.
3. Compile the candidate with
   `powershell -NoProfile -ExecutionPolicy Bypass -File
   scripts/ffmpeg/build_profile_b_msys2.ps1 -InstallDependencies`. The runner
   locates MSYS2/UCRT64, can install required `pacman` packages, clones the
   configured FFmpeg source ref, runs `configure-profile-b.sh`, copies the
   required adjacent UCRT64 runtime DLLs for `libmp3lame`, `libopus`, and
   `libwinpthread` into the produced `bin` directory, writes
   `profile-b-msys2-build-report.json`, and then runs the manifest,
   fixture-smoke, and media-preparation gates against the produced `bin`
   directory. Use `-PlanOnly` to emit the exact commands and required packages
   before touching the toolchain.
4. Run the candidate through `scripts/ffmpeg/validate_ffmpeg_profile.py`.
   The validator writes `ffmpeg-profile-manifest.json` with:
   - configure flags,
   - `ffmpeg -buildconf`,
   - `ffmpeg -version`,
   - `ffprobe -version`,
   - binary sizes,
   - enabled encoders/decoders/demuxers/muxers,
   - filters and protocols that are visible through portable FFmpeg CLI lists,
   - required MP3, WebM/Opus and stdout PCM support,
   - GPL/nonfree/network/excluded-feature warnings,
   - SHA256 for `ffmpeg.exe` and `ffprobe.exe`.
   The build-kit plan records the intended FFmpeg source URL and git ref; the
   final produced binary must still retain the exact source/ref in release
   evidence.
5. Feed the resulting directory through existing
   `scripts/build_tauri_backend_sidecar.ps1 -MediaToolsDir <dir>
   -ValidateSlimMediaTools`.
6. Run `scripts/ffmpeg/smoke_profile_b_fixtures.py --media-tools-dir <dir>
   --require-ffprobe`.
7. Run `scripts/smoke_media_preparation.py --media-tools-dir <dir>
   --require-ffprobe`.
8. Run installed-package media smoke before accepting the profile in release.

Do not commit large binaries unless the repository later defines a vendor-binary
policy.

## Packaging Integration

Current packaging is suitable:

- `scripts/build_tauri_backend_sidecar.ps1 -BundleMediaTools` copies tools into
  `tools\ffmpeg` inside the PyInstaller onedir sidecar.
- When an explicit slim `MediaToolsDir` contains adjacent runtime DLLs, the
  sidecar build copies those DLLs before validating `ffmpeg.exe` and
  `ffprobe.exe`, so UCRT64 codec DLL dependencies are preserved in the packaged
  app.
- `-ValidateSlimMediaTools` now also runs
  `scripts/ffmpeg/validate_ffmpeg_profile.py --profile B` and writes
  `tools\ffmpeg\ffmpeg-profile-manifest.json` beside the bundled binaries.
- Tauri bundles `Frontend/src-tauri/target/release/backend/` as app resources.
- `src/runtime/media_tools.py` resolves explicit env vars first, then
  `SCRIBER_MEDIA_TOOLS_DIR`, bundled app paths, and finally PATH.
- Production does not need global PATH when bundled tools are present.
- `SCRIBER_FFMPEG_PATH`, `SCRIBER_FFPROBE_PATH`, and
  `SCRIBER_MEDIA_TOOLS_DIR` remain dev/diagnostic overrides.

The Rust/Tauri side does not spawn FFmpeg directly today. Python owns media
processing.

## Test Plan

Automated tests now cover:

- shared FFmpeg command shape and URL rejection,
- user-friendly FFmpeg failure classification,
- Azure MAI non-MP3 file and live-buffer preparation as MP3, not WAV,
- sidecar slim validation requiring `libopus`, `libmp3lame`, and `pcm_s16le`,
- profile manifest validation for encoders, decoders, demuxers, muxers,
  filters, protocols, sizes, hashes, and licensing-sensitive build flags,
- Profile B build-kit generation with configure args aligned to the validator
  requirements and no network/GPL/nonfree/video/hardware flags,
- MSYS2/UCRT64 Profile-B build runner plan, package list, generated build-kit
  integration, and post-build gate orchestration,
- Profile B fixture smoke covering MP3 CBR/VBR, WAV PCM variants, MOV/M4A/MP4,
  WebM/Opus, MKV/WebM video audio extraction, OGG/Opus, FLAC, yt-dlp-like
  M4A/WebM/merged MP4, Azure-MAI MP3 preparation, WebM-to-PCM pipe output,
  raw-PCM-to-MP3 pipe output, no-audio failure, corrupted-input failure, and
  long/unicode-ish paths,
- media-smoke expectations for WebM/Opus and Azure MAI MP3 preparation,
- release-readiness media report validation.

Parser coverage note: FFmpeg does not expose a portable parser-list command
equivalent to `-encoders` or `-demuxers`. Parser requirements stay in this
strategy and candidate configure lines, but automated acceptance relies on
configure/buildconf evidence plus functional media-smoke fixtures instead of a
non-portable `ffmpeg -parsers` command.

Automated Profile-B fixture matrix for a real custom build:

- MP3 CBR
- MP3 VBR
- WAV PCM 16-bit input
- WAV PCM 24-bit input
- WAV float input
- MOV with AAC audio
- MOV/M4A with ALAC audio
- MP4/M4A with AAC audio
- WebM with Opus audio
- MKV/WebM video with audio extraction
- OGG/Opus
- FLAC input
- yt-dlp downloaded YouTube M4A
- yt-dlp downloaded YouTube WebM/Opus
- yt-dlp merged MP4
- no-audio video
- corrupted input
- filename with spaces
- filename with German umlauts
- long Windows path
- missing ffmpeg
- missing ffprobe
- timeout/cancellation

`scripts/ffmpeg/smoke_profile_b_fixtures.py` covers the media-format,
long-path, no-audio, corrupted-input, missing-tool, and timeout/cancellation
surfaces. Unsupported-codec behavior remains a diagnostic extension rather
than a hard pass/fail because broad fallback builds may legitimately decode
more codecs than the strict Profile-B target.

## Real Profile-B Build Evidence

Command:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\ffmpeg\build_profile_b_msys2.ps1 `
  -BuildRoot build\ffmpeg-profile-b-msys2 `
  -Msys2Root C:\msys64 `
  -InstallDependencies `
  -ForceClean
```

Result on 2026-06-09:

- `ok=true`, duration `323.34 s`, FFmpeg source `https://git.ffmpeg.org/ffmpeg.git`, ref `n7.0`.
- `ffmpeg.exe`: `2.55 MiB`, SHA256 `f33537a15a696af9f7d8723f09ad3b0eb3dc95834cf41764301b304c43ae1049`.
- `ffprobe.exe`: `2.43 MiB`, SHA256 `aaf9722efbad5bd3cdc53796e1f0b8591df30230d14d109041795edd7e328089`.
- Runtime DLLs copied beside the tools: `libmp3lame-0.dll` `0.31 MiB`, `libopus-0.dll` `0.47 MiB`, `libwinpthread-1.dll` `0.06 MiB`.
- Manifest gate: `FFmpeg profile B OK`, media tools `4.98 MiB`, LGPL build, no FFmpeg network protocols, MP3/WebM/Opus/raw-PCM pipe requirements present.
- Fixture smoke: `26/26` passed, including `webm_opus_to_pcm_pipe` and `raw_pcm_pipe_to_mp3`.
- Media-preparation smoke: `5/5` passed, including Azure MAI preparation as `audio/mpeg`.
- Sidecar candidate gate:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\build_tauri_backend_sidecar.ps1 `
  -SkipFrontendBuild `
  -BundleMediaTools `
  -ValidateSlimMediaTools `
  -MediaToolsDir build\ffmpeg-profile-b-msys2\dist\scriber-ffmpeg-profile-b\bin `
  -CopyToTauriRelease
```

The sidecar gate passed, copied the three DLLs plus `ffmpeg.exe` and
`ffprobe.exe` into `dist\tauri-sidecar\scriber-backend\tools\ffmpeg`, validated
the profile manifest there, and copied the sidecar to
`Frontend\src-tauri\target\release\backend`.

Additional validation against `Frontend\src-tauri\target\release\backend`:
Profile-B manifest passed, media-preparation smoke passed `5/5`, and the
runtime-dependency footprint gate passed with Backend `254.42 MiB`,
`_internal` `221.06 MiB`, `tools\ffmpeg` `5.84 MiB` including the profile
manifest, and PySide6 `71.71 MiB`.

Full NSIS release-candidate build:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\build_windows.ps1 `
  -FastLocalInstaller `
  -MediaToolsDir build\ffmpeg-profile-b-msys2\dist\scriber-ffmpeg-profile-b\bin `
  -ValidateSlimMediaTools `
  -MaxBackendRuntimeDependencyMB 325 `
  -MaxMediaToolsRuntimeDependencyMB 10 `
  -InstallerMaxInstalledSizeMB 360 `
  -RunInstallerFrontendSmoke `
  -RunInstallerMediaPreparationSmoke
```

Result on 2026-06-09:

- Build `ok=true`; installer
  `Frontend\src-tauri\target\release\bundle\nsis\Scriber_0.1.0_x64-setup.exe`
  is `102.96 MiB`.
- Installed-package smoke `ok=true`; temporary installed app measured
  `267.28 MiB` under the `360 MiB` gate.
- Installed frontend smoke passed: backend served the bundled entrypoint,
  `6/6` JS/CSS assets were verified, Tauri-origin CORS passed, and the WebView
  posted the tokenized `/api/runtime/frontend-ready` beacon.
- Installed media-preparation smoke passed `5/5`: upload compression, video
  audio extraction, YouTube post-download normalization, Azure MAI preparation
  as `audio/mpeg`/MP3, and `ffprobe` duration probing.
- Release runtime-footprint gate passed with Backend `254.42 MiB`, `_internal`
  `221.06 MiB`, `tools\ffmpeg` `5.84 MiB`, PySide6 `71.71 MiB`,
  Google/gRPC `11.37 MiB`, Pillow `4.99 MiB`, ONNXRuntime `33.75 MiB`, and
  SciPy `0.00 MiB`.
- Build timing: `879675 ms` total; `769993 ms` Tauri/NSIS bundle,
  `456418 ms` sidecar section with PyInstaller cache miss, and `95403 ms`
  installed-package smoke.

Real installed workflow smoke:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\smoke_windows_installer.ps1 `
  -InstallerPath Frontend\src-tauri\target\release\bundle\nsis\Scriber_0.1.0_x64-setup.exe `
  -LegacyDataDir "$env:LOCALAPPDATA\Scriber" `
  -VerifyLegacyDataMigration `
  -VerifyFrontend `
  -VerifyMediaPreparation `
  -VerifyRealMediaWorkflows `
  -MaxInstalledSizeMB 360 `
  -RealWorkflowYoutubeUrl "https://www.youtube.com/watch?v=0wEjbSYNUM8" `
  -RealWorkflowFileTimeoutSec 240 `
  -RealWorkflowYoutubeTimeoutSec 480 `
  -RealWorkflowPollSec 3 `
  -OutputPath tmp\installer-smoke-profile-b-real-workflows.json
```

Result on 2026-06-09: `ok=true`.

- Installed app size stayed `267.28 MiB` under the `360 MiB` gate.
- Installed frontend smoke passed and the WebView readiness beacon reached the
  tokenized backend.
- Installed media-preparation smoke passed `5/5`.
- Real file workflow passed in `154995.291 ms`: generated speech WAV uploaded,
  Azure MAI preparation transcoded the non-MP3 input to MP3, transcript
  `872a662399a245eb98a9f937733ace8d` completed with `104` content chars and a
  completed `2731`-char summary.
- Real YouTube workflow for `https://www.youtube.com/watch?v=0wEjbSYNUM8`
  passed in `100438.047 ms`: download/normalization/transcription completed as
  transcript `d91e14ebf77c4de7843bea1e8d5480b2` with `503` content chars and a
  completed `2568`-char summary.
- The combined real workflow smoke reported `2` workflows, `2` passed,
  `0` failed, and total duration `255497.404 ms`.

## Measurement Notes

Local 20-second speech estimate, 16 kHz mono:

| Format | Size | Encode time | Upload @ 10 Mbit/s | Encode + upload |
| --- | ---: | ---: | ---: | ---: |
| MP3 64k | 157.3 KB | 88.5 ms | 126 ms | about 215 ms |
| FLAC | 330.2 KB | 67.7 ms | 264 ms | about 330 ms |

Conclusion: FLAC can encode quickly, but MP3 wins on upload latency. This
matters more as duration grows, so MP3 stays in the production profile.

## Licensing Notes

- Avoid `--enable-gpl` and `--enable-nonfree` in the custom production build
  unless the project explicitly accepts the tradeoff.
- `libopus` is required for WebM/Opus output.
- `libmp3lame` is required for low-latency Azure MAI upload preparation.
- Do not include x264/x265/fdk-aac/CUDA/NVENC/QSV/Vulkan/OpenCL unless a
  separate workflow proves it is needed and licensing is reviewed.
- Retain FFmpeg source/configure-line/buildconf/version information for the
  shipped build.
- Final legal compliance and notices require human legal review.

## Rollback Plan

If the custom slim build fails real-world media workflows:

1. Rebuild with Gyan Essentials via `-UseGyanFfmpegEssentials`.
2. Keep `-ValidateSlimMediaTools` and media smokes enabled for the fallback.
3. Restore the current broad media-tools directory in the release workflow.
4. Preserve the central command builders and URL rejection; those are safe even
   with a broad FFmpeg build.

## Recommendation

Use Profile B as the production release target: local-media-only FFmpeg,
`ffprobe` included, `libopus` for WebM/Opus, `libmp3lame` for Azure MAI MP3
64k, common audio/container decoders for MP3/WAV/MOV/MP4/M4A/WebM/MKV/OGG/FLAC,
no `ffplay`, no FFmpeg network protocols, no video encoders, no hardware
stacks, and no GPL/nonfree libraries unless later proven necessary. Keep Gyan
Essentials available as the conservative rollback/local direct-build path.
