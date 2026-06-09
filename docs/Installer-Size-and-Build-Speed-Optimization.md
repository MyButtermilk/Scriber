# Installer-Größe und Build-Zeit optimieren

Zuletzt geprüft: 2026-06-09

Dieses Dokument bewertet Optimierungen für die Größe des Windows-Installers, die Größe der installierten App und die Dauer des Installer-Builds. Der Maßstab ist strikt: Der Standard-Installer muss das Tauri-Frontend und das Python-Backend vollständig funktionsfähig ausliefern. Es gibt keine optionalen Installationsbestandteile, keine Lite-Version und keine Feature-Splits.

## Aktuelle Baseline

Der aktuelle Release-Snapshot zeigt:

- Installer: ca. `188.17 MiB`
- Installiertes Backend-Verzeichnis: ca. `523.03 MiB`
- Größte installierte Backend-Bereiche:
  - `tools/ffmpeg`: ca. `267.01 MiB`
  - `_internal`: ca. `228.52 MiB`
  - `scriber-backend.exe`: ca. `27.51 MiB`

Wichtige gemessene Dependency-Gruppen im installierten Backend:

| Komponente | Installierte Größe | Bewertung |
| --- | ---: | --- |
| `tools/ffmpeg` | `267.01 MiB` | Enthält `ffmpeg.exe` und `ffprobe.exe`; größter Größenblock. |
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

Umsetzungsoptionen:

- `scripts/create_release_size_report.py` um benannte Component-Budgets erweitern.
- Oder `scripts/analyze_backend_runtime_dependencies.py` von SciPy/ONNXRuntime zu einem allgemeinen Dependency-Footprint-Report ausbauen.

Die zweite Option ist besser, wenn Component-Failures Teil der Release-Readiness sein sollen.

### P0: Slim-FFmpeg und Slim-FFprobe validieren

Das ist der größte potenzielle Größenhebel ohne Funktionsverlust.

Benötigte Fähigkeiten:

- WebM/Opus-Verarbeitung
- MP3-Verarbeitung
- AAC-/Opus-/MP3-Decoding
- MP4/M4A-, WebM/Matroska-, MP3- und WAV-Demuxing
- WebM- und MP3-Muxing
- ffprobe-Dauer- und Stream-Probing

Pflicht-Gates:

- `scripts/build_tauri_backend_sidecar.ps1 -ValidateSlimMediaTools -MediaToolsDir <candidate>`
- `scripts/smoke_media_preparation.py --media-tools-dir <bundled-tools> --require-ffprobe`
- installierter YouTube-Workflow-Smoke
- installierter File-Workflow-Smoke für Audio- und Video-Dateien

Ein Slim-Media-Build darf nicht akzeptiert werden, nur weil `ffmpeg -version` funktioniert. Die realen Media-Preparation-Hilfspfade müssen mit den tatsächlich gebündelten Binaries bestehen.

### P1: PySide6-Daten gezielt reduzieren

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

### P1: Sidecar-Hash-Cache für schnellere lokale Installer-Builds

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

### P1: Build-Timing-Metadaten ergänzen

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

Ausgabeziel: `release-metadata/build-timing.json`.

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

1. Component-Size-Budgets und Reporting ergänzen.
2. Build-Timing-Metadaten ergänzen.
3. Sidecar-Hash-Cache für schnelle lokale Installer-Builds ergänzen.
4. Schlankes `ffmpeg` plus `ffprobe` hinter den vorhandenen Media-Smoke-Gates testen.
5. PySide6-Pruning testen, ohne PySide6 selbst zu entfernen.

Diese Reihenfolge verbessert zuerst Messbarkeit, dann Build-Zeit und danach installierte Größe. Sie verhindert, dass blind optimiert oder ein funktionierendes Feature durch einen Fallback ersetzt wird.
