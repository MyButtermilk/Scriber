# Installer-Größe und Build-Zeit optimieren

Zuletzt geprüft: 2026-06-09

Dieses Dokument bewertet Optimierungen für die Größe des Windows-Installers, die Größe der installierten App und die Dauer des Installer-Builds. Der Maßstab ist strikt: Der Standard-Installer muss das Tauri-Frontend und das Python-Backend vollständig funktionsfähig ausliefern. Es gibt keine optionalen Installationsbestandteile, keine Lite-Version und keine Feature-Splits.

## Aktuelle Baseline

Der letzte Full-FFmpeg-Release-Snapshot zeigte:

- Installer: ca. `188.17 MiB`
- Installiertes Backend-Verzeichnis: ca. `523.03 MiB`
- Größte installierte Backend-Bereiche:
  - `tools/ffmpeg`: ca. `267.01 MiB`
  - `_internal`: ca. `228.52 MiB`
  - `scriber-backend.exe`: ca. `27.51 MiB`

Status 2026-06-09: Profile B ist jetzt der Standardpfad für GitHub-Windows-Release, `scripts/build_windows.ps1 -FastLocalInstaller` und direkte lokale Tauri-Builds. `scripts/build_tauri_backend_sidecar.ps1 -UseProfileBFfmpeg` verwendet vorhandene geprüfte Binaries aus `build\ffmpeg-profile-b-msys2\dist\scriber-ffmpeg-profile-b\bin` wieder oder baut sie über MSYS2/UCRT64 neu. Gyan `release essentials` bleibt als expliziter Fallback über `-UseGyanFfmpegEssentials` verfügbar; der gemessene Gyan-Kandidat liegt bei `193.32 MiB` und dient nur noch als Vergleichs-/Rollback-Evidenz.

Standard seit 2026-06-09: ein echter MSYS2/UCRT64 Custom-FFmpeg Profile-B-Build mit MP3-Latenzpfad. Der Profile-B-Pfad bestand Frontend-Typecheck, Tauri/NSIS-Bundle, Media-Preparation-Smoke, Runtime-Dependency-Footprint, Release-Metadata, Updater-Metadata-Validierung, Release-Size-Report, installierte Frontend- und Media-Smokes sowie reale installierte File- und YouTube-Transkriptionsworkflows mit abgeschlossenen Zusammenfassungen. Der aktuelle Standard-Fast-Local-Installer mit `-UseProfileBFfmpeg` misst Installer `102.98 MiB`, installierte App im Smoke `267.28 MiB`, Backend-Resource-Tree `254.42 MiB` und installierte `tools\ffmpeg` `5.84 MiB` inklusive `ffmpeg.exe`, `ffprobe.exe`, drei Runtime-DLLs und `ffmpeg-profile-manifest.json`. Ein direkter Sidecar-Wiederholungslauf mit `-UseProfileBFfmpeg -ValidateSlimMediaTools -ReuseSidecarIfUnchanged` meldete danach `cacheHit=true` mit Cache-Key `e360b748323c9c8b7311577c6df04a999bb73c541c1468fda9b6dd13f89552a8`.

Wichtige gemessene Dependency-Gruppen im installierten Backend:

| Komponente | Installierte Größe | Bewertung |
| --- | ---: | --- |
| `tools/ffmpeg` | `5.84 MiB` Profile B im installierten Smoke; `193.32 MiB` Gyan-Fallback | Profile B ist der Standard für Release, FastLocal und direkte Tauri-Builds; Gyan Essentials bleibt ein expliziter Fallback. |
| `_internal/PySide6` | `71.71 MiB` | Wird für das aktuelle hochwertige native Mic-Overlay benötigt. |
| `_internal/onnxruntime` | `33.75 MiB` | Wird für Pipecat Silero VAD benötigt. |
| `_internal/numpy.libs` | `19.99 MiB` | Enthält OpenBLAS-Runtime; das ist nicht das entfernte SciPy-Paket. |
| `_internal/PIL` | `12.46 MiB` | Wird für UI-/Export-/Bildpfade im Backend benötigt. |
| `_internal/grpc` | `10.12 MiB` | Wird durch Provider-Stacks eingebracht. |
| `_internal/google` | `1.25 MiB` | Provider-Code und Provider-Daten. |
| `_internal/yt_dlp` | `0.02 MiB` | Aktuell kein relevanter Größenblock. |

PySide6-Unterbestandteile, die sich für eine gezielte Prüfung eignen:

| PySide6-Datei oder Gruppe | Installierte Größe | Entscheidung |
| --- | ---: | --- |
| `opengl32sw.dll` | `19.68 MiB` | Nur entfernen, wenn Overlay-Smokes auf Zielsystemen zeigen, dass Qt stabil rendert. |
| `translations/` | `6.18 MiB` | Kandidat, falls das Overlay keine Qt-Übersetzungen benötigt. |
| `plugins/` | `5.02 MiB` | Kandidat für selektives Pruning ungenutzter Plugins. |
| `Qt6Core.dll`, `Qt6Gui.dll`, `Qt6Widgets.dll` | `24.98 MiB` zusammen | Behalten. Das sind Kernabhängigkeiten des Overlays. |

## Umsetzungsstand

Status 2026-06-09:

- `scripts/analyze_backend_runtime_dependencies.py` ist vom reinen SciPy/ONNXRuntime-Gate zu einem Component-Footprint-Gate erweitert. Es reportet und budgetiert jetzt zusätzlich den kompletten Backend-Sidecar, `_internal`, `tools/ffmpeg`, `PySide6` und Google/gRPC.
- `packaging/scriber-backend.spec` schließt ungenutzte Pillow-AVIF-Unterstützung (`PIL.AvifImagePlugin`, `PIL._avif`) aus. Der Code nutzt Pillow für PNG/ICO-Tray- und Legacy-Fallback-Bildpfade, aber keine AVIF-Dateien; `_internal/PIL/_avif...pyd` lag zuvor bei ca. `7.47 MiB`.
- `scripts/analyze_backend_runtime_dependencies.py` prüft zusätzlich die Pillow-Komponente und lehnt gebündelte AVIF-Binaries als disallowed ab.
- `scripts/build_windows.ps1 -RunRuntimeDependencyFootprint` leitet neue harte Budgets weiter: `-MaxBackendRuntimeDependencyMB`, `-MaxInternalRuntimeDependencyMB`, `-MaxMediaToolsRuntimeDependencyMB`, `-MaxPySide6RuntimeDependencyMB`, `-MaxGoogleGrpcRuntimeDependencyMB` und `-MaxPillowRuntimeDependencyMB`.
- `scripts/build_windows.ps1` schreibt am Ende jedes erfolgreichen Builds `release-metadata/build-timing.json`; darin stehen die Windows-Build-Phasen und, falls vorhanden, die Sidecar-Build-Metadaten.
- `scripts/build_tauri_backend_sidecar.ps1` schreibt `sidecar-build-metadata.json` mit Sidecar-Phasenzeiten, Cache-Status, kopierten Media-Tools und PySide6-Pruning-Evidenz.
- `scripts/build_tauri_backend_sidecar.ps1 -ReuseSidecarIfUnchanged` aktiviert einen Hash-Cache für lokale Sidecar-Rebuilds. Der Cache-Key berücksichtigt Backend-Quellen, Spec, Requirements, Build-Skripte, Python/PyInstaller-Version, Frontend-Dist, Media-Tool-Metadaten und relevante Build-Flags. Normale Input-Dateien werden content-basiert über `length + sha256` gehasht; mtimes zählen nur für Tool-Metadaten, damit unveränderte Vite-/Frontend-Artefakte den Sidecar-Cache nicht allein durch neue Schreibzeiten invalidieren.
- `scripts/build_tauri_backend_sidecar.ps1` unterstützt explizite PySide6-Pruning-Experimente über `-PrunePySide6Translations`, `-PrunePySide6UnusedPlugins` und `-PrunePySide6SoftwareOpenGl`. Diese Schalter sind nicht Standard und müssen mit installierten Live-Mic-Overlay-Smokes bewiesen werden.
- `scripts/prepare_gyan_ffmpeg_essentials.ps1` bleibt der explizite Fallback-Downloader für Windows-Media-Tools. Er lädt Gyan `ffmpeg-release-essentials.zip`, verifiziert die `.sha256` und liefert einen validierten `MediaToolsDir`.
- `Frontend/src-tauri/tauri.conf.json` ruft den Sidecar-Build für direkte lokale `npm run tauri:build -- --bundles nsis`-Builds jetzt mit `-UseProfileBFfmpeg -ValidateSlimMediaTools -ReuseSidecarIfUnchanged` auf. Dadurch nutzen lokale Direktbuilds denselben Profile-B-Standard wie Release/FastLocal und kopieren nicht versehentlich einen großen System-/Chocolatey-Full-Build.
- `.github/workflows/release-windows.yml` baut jetzt Profile B reproduzierbar über `scripts/ffmpeg/build_profile_b_msys2.ps1`, exportiert den erzeugten `MediaToolsDir` und übergibt ihn mit `-ValidateSlimMediaTools` an `scripts/build_windows.ps1`.
- `scripts/ffmpeg/validate_ffmpeg_profile.py` schreibt ein strukturiertes `ffmpeg-profile-manifest.json` für Profile-B-Kandidaten. Der Sidecar-Build führt es bei `-ValidateSlimMediaTools` automatisch aus und legt das Manifest neben `ffmpeg.exe` und `ffprobe.exe` in `tools\ffmpeg` ab.
- `scripts/ffmpeg/create_profile_b_build_kit.py` erzeugt einen reproduzierbaren Profile-B-Build-Kit mit `configure-profile-b.args`, `configure-profile-b.sh` und `profile-b-build-plan.json`. Der Kit enthält MP3-/Opus-/PCM-/Pipe-Pflichtflags, aber keine Netzwerk-, GPL-, nonfree-, Video-Encoder- oder Hardware-Flags.
- `scripts/ffmpeg/build_profile_b_msys2.ps1` ist der Windows-Compile-Runner für echte Profile-B-Kandidaten. Er nutzt MSYS2/UCRT64, kann die benötigten `pacman`-Pakete installieren, klont den FFmpeg-Quellstand, führt den Build-Kit aus und startet danach Manifest-, Fixture- und Media-Smoke-Gates. `-PlanOnly` schreibt denselben Ablauf als JSON, ohne eine Toolchain zu verändern.
- `scripts/ffmpeg/smoke_profile_b_fixtures.py` ist die automatisierte Profile-B-Fixture-Matrix für spätere Custom-Binaries. Sie prüft reale MP3/WAV/MOV/M4A/MP4/WebM/MKV/OGG/FLAC- und yt-dlp-ähnliche Fixtures, Azure-MAI-MP3-Vorbereitung, PCM-Pipe-Ausgabe sowie No-Audio-/Corrupt-Fehlerfälle.
- `scripts/build_windows.ps1` kann `-MediaToolsDir <path>`, `-ReuseSidecarIfUnchanged` und die PySide6-Pruning-Schalter temporär in Tauri `beforeBundleCommand` injizieren und stellt `tauri.conf.json` danach wieder her. Der explizite `-MediaToolsDir`-Pfad wurde mit dem Profile-B-Build gegen einen vollständigen NSIS-Installer inklusive installierter Frontend-/Media-Smokes validiert.

Realitätscheck gegen den aktuellen Release-Backend-Ordner:

```powershell
python scripts\analyze_backend_runtime_dependencies.py `
  --sidecar-dir Frontend\src-tauri\target\release\backend `
  --output tmp\runtime-dependency-footprint-components.json `
  --max-scipy-mb 0.001 `
  --max-onnxruntime-mb 40 `
  --max-media-tools-mb 10 `
  --max-pyside6-mb 80 `
  --max-google-grpc-mb 15 `
  --max-pillow-mb 6 `
  --max-internal-mb 250 `
  --max-backend-mb 325
```

Der Check bestand vor der Essentials-Umstellung nach Pillow-AVIF-Pruning mit den Full-FFmpeg-Messwerten: Backend `515.57 MiB`, `_internal` `221.05 MiB`, Media-Tools `267.01 MiB`, PySide6 `71.71 MiB`, Google/gRPC `11.37 MiB`, Pillow `4.99 MiB`, ONNXRuntime `33.75 MiB`, SciPy `0.00 MiB`. Nach der Essentials-Umstellung bestand der Footprint-Check mit Backend `441.88 MiB`, `_internal` `221.05 MiB`, Media-Tools `193.32 MiB`, PySide6 `71.71 MiB`, Google/gRPC `11.37 MiB`, Pillow `4.99 MiB`, ONNXRuntime `33.75 MiB`, SciPy `0.00 MiB`; diese Werte sind nur noch Fallback-/Vergleichsevidenz. Mit Profile B bestand derselbe Gate mit Standardbudgets: Backend `254.42 MiB`, `_internal` `221.06 MiB`, Media-Tools `5.84 MiB`, PySide6 `71.71 MiB`, Google/gRPC `11.37 MiB`, Pillow `4.99 MiB`, ONNXRuntime `33.75 MiB`, SciPy `0.00 MiB`; verwendete Budgets waren Backend `325 MiB` und Media-Tools `10 MiB`.

Vollstaendiger NSIS-Realitaetscheck am 2026-06-09:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\build_windows.ps1 `
  -SkipChecks `
  -SkipSmoke `
  -ReuseSidecarIfUnchanged `
  -RunMediaPreparationSmoke `
  -RunRuntimeDependencyFootprint `
  -MaxScipyRuntimeDependencyMB 0.001 `
  -MaxOnnxRuntimeDependencyMB 40 `
  -MaxPythonRuntimeDependencyMB 40 `
  -MaxBackendRuntimeDependencyMB 325 `
  -MaxInternalRuntimeDependencyMB 250 `
  -MaxMediaToolsRuntimeDependencyMB 10 `
  -MaxPySide6RuntimeDependencyMB 80 `
  -MaxGoogleGrpcRuntimeDependencyMB 15 `
  -MaxPillowRuntimeDependencyMB 6
```

Ergebnis der historischen Full-/Gyan-Vergleichsbuilds: Full-FFmpeg lag bei Installer `186.41 MiB`, Backend `515.56 MiB`; Gyan Essentials lag bei Installer `152.98 MiB`, Backend `441.88 MiB` und Media-Tools `193.32 MiB`. Der aktuelle Standard ist Profile B mit den engeren Budgets oben.

`release-metadata/build-timing.json` meldet fuer diesen Clean-Release-Pfad `590451 ms` Gesamtzeit. Davon entfallen `584948 ms` auf `Tauri Windows bundle`; im eingebetteten Sidecar-Timing stehen `223361 ms` Gesamtzeit, `177740 ms` PyInstaller, `19066 ms` Copy-to-Tauri-Release und `16171 ms` Cache-Save. Der lokale Cache war in diesem konkreten NSIS-Build ein Miss, weil sich Build-Inputs geaendert hatten; der identische Sidecar-Only-Lauf bleibt unten als Cache-Hit-Evidenz erhalten.

Anschliessende `scripts\build_windows.ps1 -FastLocalInstaller`-Realbuilds liefen ebenfalls erfolgreich durch: Frontend-Typecheck, Tauri/NSIS-Bundle, Media-Preparation-Smoke, Runtime-Dependency-Footprint, Release-Metadata, Updater-Metadata-Validierung und Release-Size-Report waren gruen. Der aktuelle Fast-Local-Modus setzt ohne explizites `-MediaToolsDir` automatisch `-UseProfileBFfmpeg`, `-ValidateSlimMediaTools`, `-ReuseSidecarIfUnchanged`, Backend-Budget `325 MiB` und Media-Tools-Budget `10 MiB`. Damit bleibt der lokale Installer ein vollständiger Installer ohne Feature-Split; nur die Iterations-Gates sind schlanker.

Profile-B-Fast-Local-Installer mit MP3-Latenzpfad:

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

Ergebnis: `ok=true`. `release-metadata/size-report.json` meldete den NSIS-Installer `Scriber_0.1.0_x64-setup.exe` mit aktuell `102.98 MiB`. Der installierte Smoke meldete `267.28 MiB` App-Größe unter dem `360 MiB` Budget, Frontend/WebView-ready, `6/6` gebündelte JS/CSS-Assets abrufbar, installierte Media-Tools `5/5` Checks und Silent-Uninstall erfolgreich. Der installierte Media-Smoke bestätigte Upload-Kompression, Video-Audio-Extraktion, YouTube-Post-Download-Normalisierung, Azure-MAI-Vorbereitung als `audio/mpeg`/MP3 und `ffprobe`-Dauerprüfung. `runtime-dependency-footprint.json` meldete Backend `254.42 MiB`, `_internal` `221.06 MiB`, `tools\ffmpeg` `5.84 MiB`, PySide6 `71.71 MiB`, Google/gRPC `11.37 MiB`, Pillow `4.99 MiB`, ONNXRuntime `33.75 MiB`, SciPy `0.00 MiB` und keine Budget-Failures. `build-timing.json` meldete beim aktuellen Standard-Fast-Local-Build `538270 ms` Gesamtzeit; davon `465895 ms` Tauri/NSIS-Bundle, `247371 ms` Sidecar-Teil mit PyInstaller-Cache-Miss und `65033 ms` installierter Package-Smoke.

Installierter Smoke gegen den frueheren Full-/Gyan-nahen Setup-Stand am 2026-06-09:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\smoke_windows_installer.ps1 `
  -InstallerPath Frontend\src-tauri\target\release\bundle\nsis\Scriber_0.1.0_x64-setup.exe `
  -VerifyFrontend `
  -VerifyMediaPreparation `
  -VerifySupportBundle `
  -VerifyUninstall `
  -MaxInstalledSizeMB 560 `
  -OutputPath tmp\installer-smoke-current.json
```

Ergebnis: `ok=true`, installierte App `528.43 MiB` unter dem `560 MiB` Smoke-Budget, Frontend/WebView-ready verifiziert, installierte Media-Tools `5/5` Checks, Support-Bundle token-geschuetzt und redaction-verifiziert, Silent-Uninstall entfernt App-Artefakte und erhaelt Runtime-Daten-Sentinel. Live-Mic-/visueller PySide6-Overlay-Smoke wurde dabei bewusst nicht beansprucht und bleibt die Voraussetzung fuer jedes PySide6-Pruning im Standardpfad.

Frontend-Browser-Smoke am 2026-06-09:

```powershell
python scripts\smoke_frontend_browser.py --output tmp\frontend-browser-smoke-current.json
```

Ergebnis: `ok=true`, `7` Routen geprueft, `0` kritische Console Errors, `0` Page Errors, `0` unhandled Rejections. Die Interaktionschecks bestanden fuer `youtube-thumbnails` (Suche und URL-Lookup je `1/1` geladene Thumbnails), `file-drag-drop`, `debug-clear`, `transcript-processing-refresh` und `token-required-browser-state`. Damit sind die YouTube-Suche-/URL-UI-Pfade, der File-Drag-and-Drop-Pfad und der YouTube-Processing-Detailpfad gegen einen synthetischen Backend-Vertrag abgedeckt.

Der Sidecar-Cache wurde real geprüft:

- erster Lauf mit `-ReuseSidecarIfUnchanged -BundleMediaTools -CopyToTauriRelease`: `cacheHit=false`, PyInstaller baute den Sidecar und füllte den Cache.
- zweiter identischer Lauf nach content-basiertem Cache-Key-Fix: `cacheHit=true`, keine PyInstaller-Phase, gleicher Cache-Key `e360b748323c9c8b7311577c6df04a999bb73c541c1468fda9b6dd13f89552a8`, `totalDurationMs=51429`; die verbleibenden Phasen waren Profile-B-Prepare, Import-Preflight, Cache-Key, Cache-Restore, Frozen-Import-Check und Release-Copy.

## No-Feature-Loss-Entscheidungen

### PySide6 bleibt im Standard-Installer

`PySide6` darf nicht vollständig aus dem Standard-Tauri-Installer entfernt werden.

`src/overlay.py` nutzt PySide6 ausdrücklich als bevorzugten Renderer für das native Aufnahme-Overlay. tkinter ist nur ein Fallback. Das Entfernen von PySide6 würde zwar einen Fallback übrig lassen, wäre aber keine Optimierung ohne Funktionsverlust, weil das aktuelle glatte transparente Mic-Overlay und die native Audio-Visualisierung PySide6-basiert sind.

Empfohlene sichere Richtung:

- `PySide6-Essentials` vorerst behalten.
- Gezieltes Pruning von `opengl32sw.dll`, Qt-Übersetzungen und ungenutzten Qt-Plugins prüfen.
- Eine PySide6-Pruning-Änderung nur akzeptieren, wenn der installierte Build Live Mic, Overlay, Stop-Button, Initializing-State, Transcribing-State und Waveform-Updates korrekt zeigt.

### FFmpeg und FFprobe bleiben im Standard-Installer

`ffmpeg` und `ffprobe` dürfen nicht aus dem Standard-Installer entfernt werden.

Das Backend nutzt Media-Tools für File-Upload-Kompression, Audio-Extraktion aus Videos, YouTube-Normalisierung, Azure-MAI-Audio-Vorbereitung und Dauer-/Stream-Probing. Die vorhandenen Skripte behandeln `-SkipBundledFfprobe` bereits als explizites Größenexperiment, nicht als Standard-Release-Pfad.

Empfohlene sichere Richtung:

- Beide Tools gebündelt behalten.
- Einen validierten schlanken `ffmpeg` plus schlanken `ffprobe` bevorzugen.
- Das vorhandene Gate `-ValidateSlimMediaTools` nutzen und nur erweitern, wenn reale Workflows weitere Codecs oder Container benötigen.
- `scripts/smoke_media_preparation.py` gegen die tatsächlich gebündelten Tools ausführen, bevor ein Slim-Media-Build akzeptiert wird.

### Provider-Abhängigkeiten bleiben, solange Provider verfügbar sind

`google-generativeai`, Provider-SDKs und Pipecat-Provider-Extras dürfen nicht entfernt werden, solange diese Provider in Settings oder Routing verfügbar bleiben.

`src/summarization.py` nutzt Gemini über REST, aber `src/gemini_transcribe.py` importiert `google.generativeai` lazy. Das Entfernen des Pakets würde diesen Provider-Pfad brechen, auch wenn Zusammenfassungen weiter funktionieren.

Empfohlene sichere Richtung:

- Provider-Abhängigkeiten im No-Feature-Loss-Installer behalten.
- Provider-Paketierung nur durch Entfernen nicht benötigter Paketdaten, Tests, Beispiele oder Metadaten optimieren.
- Für jeden eingegrenzten Provider-Pfad Frozen-Runtime-Import-Checks ergänzen.

### yt-dlp-Extractor-Filtering wird nicht Standard

YouTube-only `yt-dlp`-Extractor-Filtering darf nicht Standard werden, bevor ein Frozen-Sidecar echte YouTube-Downloads zuverlässig bestanden hat.

`yt-dlp` nutzt dynamisches Extractor-Loading. Außerdem ist das aktuell gemessene `_internal/yt_dlp`-Verzeichnis sehr klein. Das Risiko ist damit höher als der erwartbare Größen-Gewinn.

Empfohlene sichere Richtung:

- Extractor-Filtering nur als Experiment behandeln.
- Erst erneut prüfen, wenn eine vollständige Sidecar-Analyse zeigt, dass versteckte `yt-dlp`-Daten wirklich relevant groß sind.
- Vor Annahme einen installierten YouTube-Smoke mit Suche, eingefügter URL, Download, Transkription, Zusammenfassung und Thumbnail-Pfad verlangen.

### Legacy-UI- und Tray-Abhängigkeiten prüfen

Legacy-Python-UI- und Python-Tray-Abhängigkeiten dürfen aus dem Standard-Tauri-Release entfernt werden, wenn die moderne Tauri-Oberfläche und das Python-Backend alle Nutzerfunktionen behalten.

Das ist keine optionale Paketstrategie. Es bedeutet nur, dass alte UI-Implementierungsabhängigkeiten nicht im Standard-Release bleiben müssen, sobald Tauri diese Workflows vollständig besitzt.

Kandidaten für ein Audit:

- `customtkinter`
- `pystray`
- tkinter-only Legacy-Entrypoints
- PyInstaller-Hidden-Imports, die nur für `src/ui.py`, `src/main.py` oder alte Python-Tray-Pfade existieren

Akzeptanzregel: Eine Entfernung ist nur gültig, wenn kein moderner Tauri-Workflow auf die entfernte Abhängigkeit zurückfällt.

## Empfohlene Optimierungs-Roadmap

### P0: Component-Size-Budgets ergänzen

Status: umgesetzt.

Release-Gates sollen Größenregressionen sichtbar und eindeutig bewertbar machen.

Empfohlene Budgets:

- größtes Installer-Artefakt
- installierte App
- installiertes Backend-Verzeichnis
- `tools/ffmpeg`
- `_internal/PySide6`
- `_internal/onnxruntime`
- `_internal/google` plus `_internal/grpc`
- `_internal` gesamt

Umgesetzt ist `scripts/analyze_backend_runtime_dependencies.py` als allgemeiner Component-Footprint-Report für Standard-Release-Komponenten.

### P0: Gyan Essentials als Fallback-Media-Tools validieren

Status: umgesetzt und als expliziter Fallback/Rollback-Pfad gemessen. Nicht mehr Standard fuer direkte lokale Tauri-Builds.

Das war der erste konservative Größenhebel ohne Funktionsverlust und bleibt als Rückfallweg nützlich. Der größere Größenhebel ist inzwischen Profile B.

Benötigte Fähigkeiten:

- WebM/Opus-Verarbeitung
- MP3-Verarbeitung
- AAC-/Opus-/MP3-Decoding
- MP4/M4A-, WebM/Matroska-, MP3- und WAV-Demuxing
- WebM- und MP3-Muxing
- ffprobe-Dauer- und Stream-Probing

Pflicht-Gates:

- `scripts/prepare_gyan_ffmpeg_essentials.ps1`
- `scripts/build_tauri_backend_sidecar.ps1 -ValidateSlimMediaTools -MediaToolsDir <candidate>`
- `scripts/smoke_media_preparation.py --media-tools-dir <bundled-tools> --require-ffprobe`
- installierter YouTube-Workflow-Smoke
- installierter File-Workflow-Smoke für Audio- und Video-Dateien

Ein Slim-Media-Build darf nicht akzeptiert werden, nur weil `ffmpeg -version` funktioniert. Die realen Media-Preparation-Hilfspfade müssen mit den tatsächlich gebündelten Binaries bestehen.

Validierungsstand 2026-06-09:

- `scripts\prepare_gyan_ffmpeg_essentials.ps1` lief erfolgreich und verifizierte SHA256 `6f58ce889f59c311410f7d2b18895b33c03456463486f3b1ebc93d97a0f54541`.
- Der vorbereitete Gyan-Essentials-Kandidat misst `ffmpeg.exe` `96.76 MiB` und `ffprobe.exe` `96.56 MiB`; zusammen `193.32 MiB` statt `267.01 MiB` beim bisherigen Full-Build.
- Capability-Check bestanden: `libopus`, `libmp3lame`, `pcm_s16le`, AAC/Opus/MP3/FLAC/ALAC-Decoding, WebM/Matroska-, MP4/M4A-, MP3-, WAV-, OGG-, FLAC- und raw-`s16le`-Demuxing, lokale `file`-/`pipe`-Protokolle sowie WebM-/MP3-Muxing vorhanden.
- Das neue Profile-B-Manifest-Gate ist gegen den lokalen FFmpeg-Referenzpfad gelaufen und meldete `ok=true`, Media-Tools `267.01 MiB`, Pflichtfunktionen inklusive MP3 und `pcm_s16le` vorhanden; die breite Referenz erzeugt erwartete Warnungen für Netzwerkprotokolle, GPL/version3 und ausgeschlossene Video-/Hardware-Features.
- `scripts\ffmpeg\create_profile_b_build_kit.py --output-dir tmp\ffmpeg-profile-b-build-kit --source-url https://git.ffmpeg.org/ffmpeg.git --git-ref n7.0 --print-json` lief erfolgreich und schrieb die Profile-B-Configure-Dateien plus Buildplan.
- `scripts\ffmpeg\smoke_profile_b_fixtures.py --output tmp\ffmpeg-profile-b-fixtures-local.json --require-ffprobe --duration-sec 0.5` lief gegen den lokalen FFmpeg-Referenzpfad erfolgreich mit `25/25` Checks. Die aktuelle Fixture-Matrix wurde danach auf `26` Checks erweitert, weil raw-PCM-zu-MP3-Pipe explizit für Azure-MAI-Latenz abgesichert wird.
- `scripts\smoke_media_preparation.py --media-tools-dir <gyan-essentials-bin> --require-ffprobe` meldete `5/5` bestandene Checks.
- `scripts\build_tauri_backend_sidecar.ps1 -SkipFrontendBuild -BundleMediaTools -UseGyanFfmpegEssentials -ValidateSlimMediaTools -ReuseSidecarIfUnchanged -CopyToTauriRelease` lief erfolgreich, kopierte Essentials in `Frontend\src-tauri\target\release\backend\tools\ffmpeg` und schrieb `preparedMediaTools` in `sidecar-build-metadata.json`.
- `scripts\smoke_media_preparation.py --media-tools-dir Frontend\src-tauri\target\release\backend\tools\ffmpeg --require-ffprobe` meldete gegen den tatsächlich kopierten Release-Ordner `5/5` bestandene Checks.
- `scripts\analyze_backend_runtime_dependencies.py --sidecar-dir Frontend\src-tauri\target\release\backend --max-media-tools-mb 210 --max-backend-mb 500 ...` meldete `ok=true`, Backend `441.88 MiB` und Media-Tools `193.32 MiB`.
- Historischer Stand: `Frontend/src-tauri/tauri.conf.json` nutzte damals `-UseGyanFfmpegEssentials -ValidateSlimMediaTools` im direkten lokalen `beforeBundleCommand`; aktueller Stand ist `-UseProfileBFfmpeg -ValidateSlimMediaTools -ReuseSidecarIfUnchanged`.
- Installierter Smoke mit `-VerifyFrontend -VerifyMediaPreparation -VerifySupportBundle -VerifyUninstall -MaxInstalledSizeMB 500` bestand: installierte App `454.75 MiB`, Frontend/WebView-ready, installierte Media-Tools `5/5`, Support-Bundle-Redaction bestanden, Silent-Uninstall entfernt App-Artefakte und erhaelt Runtime-Daten-Sentinel.
- Der GitHub-Release-Workflow verwendet nach der Profile-B-Real-Workflow-Evidenz nicht mehr Gyan als Zielpfad.

### P0: Custom FFmpeg Profile B mit MP3-Latenzpfad

Status: echter MSYS2/UCRT64-Compile, vollständiger NSIS-/Installed-Media-Smoke und reale installierte File-/YouTube-Workflow-Smokes bestanden; als Standard-Media-Tools fuer GitHub-Windows-Release, FastLocal und direkte lokale Tauri-Builds aktiviert.

Der Custom-Build ist der groesste Groessenhebel ohne Feature-Verlust. Er behaelt `ffmpeg.exe`, `ffprobe.exe`, MP3-Encoding ueber `libmp3lame`, WebM/Opus ueber `libopus`, lokale `file`-/`pipe`-Protokolle, raw PCM in beide Pipe-Richtungen, common Audio-Container und `ffprobe`-Dauer-/Stream-Probing. Er laesst FFmpeg-Netzwerkprotokolle, `ffplay`, Video-Encoder, Hardware-Stacks, GPL- und nonfree-Flags weg.

Gemessener Stand 2026-06-09:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\ffmpeg\build_profile_b_msys2.ps1 `
  -BuildRoot build\ffmpeg-profile-b-msys2 `
  -Msys2Root C:\msys64 `
  -InstallDependencies `
  -ForceClean
```

Ergebnis:

- Build `ok=true`, Dauer `323.34 s`, Quelle `https://git.ffmpeg.org/ffmpeg.git`, Ref `n7.0`.
- Portable Media-Tools: `4.98 MiB` gesamt.
- `ffmpeg.exe`: `2.55 MiB`; `ffprobe.exe`: `2.43 MiB`.
- Runtime-DLLs: `libmp3lame-0.dll` `0.31 MiB`, `libopus-0.dll` `0.47 MiB`, `libwinpthread-1.dll` `0.06 MiB`.
- Profile-Manifest: OK, LGPL, keine FFmpeg-Netzwerkprotokolle, MP3/WebM/Opus/raw-PCM-Pipe-Funktionen vorhanden.
- Fixture-Smoke: `26/26` bestanden, inklusive `webm_opus_to_pcm_pipe` und `raw_pcm_pipe_to_mp3`.
- Media-Preparation-Smoke: `5/5` bestanden, inklusive Azure MAI Vorbereitung als `audio/mpeg`.
- Sidecar-Kandidat:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\build_tauri_backend_sidecar.ps1 `
  -SkipFrontendBuild `
  -BundleMediaTools `
  -ValidateSlimMediaTools `
  -MediaToolsDir build\ffmpeg-profile-b-msys2\dist\scriber-ffmpeg-profile-b\bin `
  -CopyToTauriRelease
```

Ergebnis: bestanden. Der Sidecar-Build kopierte `ffmpeg.exe`, `ffprobe.exe` und die drei Runtime-DLLs nach `tools\ffmpeg`, validierte dort erneut das Profile-B-Manifest und kopierte den Sidecar nach `Frontend\src-tauri\target\release\backend`.

Zusaetzliche Validierung gegen den tatsaechlich kopierten Tauri-Release-Backend-Ordner:

- `python scripts\ffmpeg\validate_ffmpeg_profile.py --media-tools-dir Frontend\src-tauri\target\release\backend\tools\ffmpeg --profile B --require-lgpl --output tmp\ffmpeg-profile-b-tauri-target-manifest.json`: bestanden, Media-Tools `4.98 MiB` ohne Manifest-Overhead.
- `python scripts\smoke_media_preparation.py --media-tools-dir Frontend\src-tauri\target\release\backend\tools\ffmpeg --require-ffprobe --output tmp\media-preparation-smoke-profile-b-tauri-target.json`: `5/5` bestanden.
- `python scripts\analyze_backend_runtime_dependencies.py --sidecar-dir Frontend\src-tauri\target\release\backend --max-media-tools-mb 10 --max-backend-mb 325 ...`: bestanden; Backend `254.42 MiB`, `_internal` `221.06 MiB`, `tools\ffmpeg` `5.84 MiB` inklusive `ffmpeg-profile-manifest.json`, PySide6 `71.71 MiB`.
- Vollständiger Fast-Local-NSIS-Build mit Profile-B-Standard, `-RunInstallerFrontendSmoke` und `-RunInstallerMediaPreparationSmoke`: bestanden; Installer `102.98 MiB`, installierte App `267.28 MiB`, Frontend/WebView-ready, installierte Media-Tools `5/5`, Azure-MAI-Vorbereitung als MP3, `ffprobe`-Dauerprüfung bestanden.
- Realer installierter Workflow-Smoke mit `-VerifyRealMediaWorkflows`: bestanden; File-Workflow `154995.291 ms` mit Azure-MAI-MP3-Vorbereitung, abgeschlossenem Transcript und `2731` Zeichen Summary; YouTube-Workflow fuer `https://www.youtube.com/watch?v=0wEjbSYNUM8` `100438.047 ms` mit Download/Normalisierung/Transkription und `2568` Zeichen Summary; Gesamt `2/2` Workflows bestanden.
- `.github/workflows/release-windows.yml` baut Profile B via MSYS2/UCRT64, validiert den Build-Report und uebergibt den erzeugten `MediaToolsDir` mit harten Budgets `325 MiB` Backend und `10 MiB` Media-Tools an `scripts/build_windows.ps1`.

Erwarteter Groesseneffekt gegen die bisherigen Media-Tools:

| Kandidat | Media-Tools installiert | Ersparnis gegen Full `267.01 MiB` | Ersparnis gegen Gyan Essentials `193.32 MiB` |
| --- | ---: | ---: | ---: |
| Full reference | `267.01 MiB` | `0 MiB` | n/a |
| Gyan Essentials | `193.32 MiB` | ca. `73.69 MiB` | `0 MiB` |
| Custom Profile B | `4.98 MiB` | ca. `262.03 MiB` | ca. `188.34 MiB` |

Akzeptanzstand:

- Profile B ist fuer GitHub-Windows-Release, FastLocal und direkte lokale Tauri-Builds akzeptiert.
- Gyan Essentials bleibt nur noch expliziter Fallback/Rollback-Pfad über `-UseGyanFfmpegEssentials`.

### P1: PySide6-Daten gezielt reduzieren

Status: Pruning-Schalter implementiert, nicht als Standard aktiviert; installierter visueller Overlay-Smoke fehlt weiterhin.

PySide6 bleibt erhalten, aber der gebündelte Qt-Baum enthält wahrscheinlich Dateien, die das Overlay nicht nutzt.

Empfohlene Prüfreihenfolge:

1. Qt-Übersetzungen
2. ungenutzte Image-Format-Plugins
3. ungenutzte TLS-/Network-Plugins, falls keine Qt-Network-Funktion genutzt wird
4. `opengl32sw.dll`, nur nach Tests auf Zielmaschinen ohne GPU-/OpenGL-Probleme

Pflicht-Gates:

- Frozen-Runtime-Import-Check
- installierter App-Start
- Live-Mic-Overlay sichtbar im Initializing-State
- Overlay sichtbar während Recording
- Waveform aktualisiert sich während Recording
- Stop-Button funktioniert
- Transcribing-Spinner erscheint
- Overlay verschwindet nach Abschluss

Wenn eine Zielmaschine `opengl32sw.dll` für zuverlässiges Qt-Rendering braucht, bleibt die Datei im Standard-Installer.

Audit 2026-06-09:

- `scripts\build_windows.ps1` und `scripts\build_tauri_backend_sidecar.ps1` koennen PySide6-Translations, ungenutzte Plugins und `opengl32sw.dll` explizit prunen.
- Vorhandene Live-Recording-Smokes pruefen Start/Stop-/Backend-Stabilitaet, aber sie beweisen nicht visuell, dass das PySide6-Overlay sichtbar ist, die Waveform aktualisiert und das Overlay nach Abschluss verschwindet.
- Deshalb wird kein PySide6-Pruning als Standard aktiviert. Der naechste sichere Schritt ist ein installierter visueller Overlay-Smoke oder eine manuelle Screenshot-Evidenz auf Zielmaschinen, erst danach darf eines der Pruning-Flags in den Standard-Release-Pfad wandern.

### P1: Sidecar-Hash-Cache für schnellere lokale Installer-Builds

Status: opt-in über `-ReuseSidecarIfUnchanged` implementiert.

Der aktuelle Tauri-Bundle-Pfad ruft `scripts/build_tauri_backend_sidecar.ps1` über `beforeBundleCommand` auf. Das Skript führt PyInstaller mit `--clean` aus. Das ist für saubere Release-Builds sinnvoll, aber für wiederholte lokale Installer-Builds teuer.

Ein Cache-Modus soll den vorhandenen PyInstaller-Sidecar wiederverwenden, wenn sich relevante Inputs nicht geändert haben.

Hash-Inputs:

- `packaging/scriber-backend.spec`
- `requirements-base.txt`
- Python-Version und PyInstaller-Version
- alle gebündelten Dateien unter `src/`
- repository-lokale Kompatibilitätspakete, die der Worker nutzt
- relevante Build-Skripte
- gebündeltes Frontend-Dist, falls es Teil des Sidecars bleibt
- Media-Tool-Pfade, Dateigrößen, mtimes und optional SHA256
- Build-Flags wie `BundleMediaTools`, `SkipBundledFfprobe`, `ValidateSlimMediaTools` und `MediaToolsDir`

Verhalten:

- Bei Cache-Hit: PyInstaller überspringen und den gecachten Sidecar nach `Frontend/src-tauri/target/release/backend` kopieren.
- Bei Cache-Miss: aktuellen sauberen PyInstaller-Build ausführen und Cache-Manifest schreiben.
- Release-Builds dürfen zunächst weiterhin Clean-Builds erzwingen, bis der Cache stabil bewiesen ist.

### P1: Fast-Local-Build und Full-Release-Build trennen

Status: umgesetzt.

Das Standard-Release-Artefakt bleibt funktional identisch, aber lokale Iteration wird schneller.

Empfohlene Modi:

- Schneller lokaler Installer:
  - gültigen Sidecar-Cache wiederverwenden
  - vollständige Python-Test-Suite optional überspringen
  - Frontend-Typecheck und Build standardmäßig behalten
  - Size-Metadaten weiter erzeugen
- Vollständiger Release-Installer:
  - sauberer Sidecar-Build
  - vollständige Tests
  - Media-Preparation-Smoke
  - Runtime-Dependency-Footprint
  - Updater-/Signing-Metadatenvalidierung, wenn konfiguriert
  - angeforderte Installed-App-Smokes

Das verbessert Build-Zeit ohne Änderung am Inhalt der installierten Standard-App.

Der schnelle lokale Pfad ist ein expliziter opt-in über `scripts/build_windows.ps1 -FastLocalInstaller`. Der Schalter aktiviert den Sidecar-Cache, überspringt die vollständige Python-Test-Suite und den Tauri-Release-Smoke, behält aber Frontend-Typecheck, Frontend-Produktionsbuild, Media-Preparation-Smoke, Runtime-Dependency-Footprint und harte Standard-Budgets bei. Damit bleibt der lokale Installer ein vollständiger Installer ohne Feature-Split; nur die Iterations-Gates sind schlanker.

`-ReuseSidecarIfUnchanged` bleibt als engerer Schalter erhalten, wenn nur der Sidecar-Cache aktiviert werden soll. Full-Release-Builds bleiben clean, solange `-FastLocalInstaller` oder `-ReuseSidecarIfUnchanged` nicht explizit gesetzt wird.

### P1: Build-Timing-Metadaten ergänzen

Status: umgesetzt.

Die Windows-Build-Ausgabe soll Phasenzeiten erfassen, damit Build-Speed-Arbeit messbar wird.

Mindestens erfassen:

- Version-Sync
- Python-Tests
- Frontend-Typecheck
- Frontend-Build
- PyInstaller Analysis/Build/Collect
- Media-Tool-Copy/Validation
- Tauri-/Rust-Build
- NSIS-Packaging
- Release-Metadaten
- Smoke-Tests

Ausgabeziel: `release-metadata/build-timing.json`. Sidecar-interne Phasen stehen zusätzlich in `target\release\backend\sidecar-build-metadata.json` und werden in den Windows-Build-Timing-Report eingebettet, wenn vorhanden.

### P1: Pillow-AVIF aus Standard-Sidecar ausschließen

Status: umgesetzt.

Pillow bleibt gebündelt, weil Tray-/Legacy-Fallback-Pfade `Image`, `ImageDraw` und `ImageTk` nutzen. AVIF-Unterstützung wird nicht benötigt und war mit `_internal/PIL/_avif...pyd` ein großer einzelner Binary-Block. `packaging/scriber-backend.spec` schließt `PIL.AvifImagePlugin` und `PIL._avif` aus; der Runtime-Footprint-Gate behandelt AVIF unter `components.pillow.disallowedPaths` als Fehler.

Pflicht-Gates:

- Frozen-Runtime-Import-Check
- Release-Footprint ohne Pillow-AVIF
- Tray-/Overlay-Smoke, wenn Legacy-Fallback-Bildpfade geändert werden

### P2: Google-Package-Daten enger sammeln

Broad `collect_data_files("google")` darf nur eingegrenzt werden, wenn Provider-Smokes keinen Runtime-Verlust zeigen.

Kandidatenrichtung:

- breite Google-Datensammlung durch gezielte Datensammlung für tatsächlich benötigte Google-/Pipecat-Provider-Pfade ersetzen.
- Code-Module und Provider-Pakete verfügbar lassen.
- Frozen-Runtime-Import-Checks für Google STT und Gemini-Transcription ergänzen.

Der erwartete Größengewinn ist kleiner als bei Media-Tools oder PySide6-Pruning, aber nach stärkeren Gates sinnvoll.

### P2: PyInstaller Strip-Settings messen

`strip=True` kann Größe reduzieren, ist auf Windows aber je nach Binary wirkungslos oder riskant.

Nur als gemessenes Release-Experiment behandeln:

- Build mit und ohne Strip erzeugen.
- Installer-Größe, installierte Größe, Startup und Smoke-Ergebnisse vergleichen.
- Einstellung nur behalten, wenn Größe sinkt und alle Frozen-Smokes bestehen.

## Nicht für das Standard-Release empfohlen

Diese Maßnahmen sollen nicht in den Standard-Installer:

- `PySide6` vollständig entfernen.
- `ffmpeg` entfernen.
- gebündeltes `ffprobe` entfernen.
- `onnxruntime` entfernen, solange Silero VAD genutzt wird.
- `google-generativeai` entfernen, solange Gemini-Transcription unterstützt wird.
- Provider-SDKs entfernen, solange deren Provider auswählbar bleiben.
- Lite-Installer mit optionalen Provider- oder Media-Packs ausliefern.
- Von systemweit installiertem `ffmpeg`, `ffprobe`, Python-Paketen oder Provider-Extras abhängen.

## Akzeptanztests

Jede akzeptierte Optimierung muss bestehen:

- `python -m pytest -q`
- Frontend-Typecheck und Produktionsbuild
- Tauri-Rust-Tests
- Frozen-Backend-Runtime-Import-Check
- Runtime-Dependency-Footprint-Report
- Release-Size-Report
- Media-Preparation-Smoke gegen die tatsächlich gebündelten Media-Tools
- Installed-App-Smoke für Frontend und Backend
- Installed-App-Support-Bundle-Smoke
- Live-Mic-Overlay-Smoke mit sichtbarem PySide6-Overlay und Waveform
- YouTube-Smoke mit eingefügter URL und Search-Result-Pfad
- File-Upload-Smoke mit mindestens einer Audio- und einer Video-Datei

Keine Optimierung ist akzeptiert, wenn eine bestehende Funktion nur noch durch manuelle Nachinstallation, optionale Downloads, System-PATH-Abhängigkeiten oder einen user-sichtbar schlechteren Fallback funktioniert.

## Empfohlener erster Umsetzungsbatch

1. Component-Size-Budgets und Reporting ergänzen. Status: umgesetzt.
2. Build-Timing-Metadaten ergänzen. Status: umgesetzt.
3. Sidecar-Hash-Cache und expliziten Fast-Local-Installer-Modus ergänzen. Status: umgesetzt.
4. Schlankes `ffmpeg` plus `ffprobe` hinter den vorhandenen Media-Smoke-Gates testen. Status: Gyan Essentials als expliziter Fallback-Build-Pfad umgesetzt; Profile B gebaut, mit NSIS-Installer, installiertem Frontend-/Media-Smoke und realem installiertem File-/YouTube-Workflow-Smoke bestanden; GitHub-Windows-Release, FastLocal und direkte lokale Tauri-Builds nutzen Profile B als Standard-Media-Tool-Input.
5. PySide6-Pruning testen, ohne PySide6 selbst zu entfernen. Status: Schalter umgesetzt; kein Standard-Pruning ohne installierten visuellen Overlay-Smoke.

Diese Reihenfolge verbessert zuerst Messbarkeit, dann Build-Zeit und danach installierte Größe. Sie verhindert, dass blind optimiert oder ein funktionierendes Feature durch einen Fallback ersetzt wird.
