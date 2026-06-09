# Scriber Hybrid-Architektur Goal

Last updated: 2026-06-03

This document is the authoritative Codex goal for the Scriber hybrid
architecture work. It replaces earlier incomplete goal text.

## Zusammenfassung

- Ziel: **React UI + Tauri/Rust Desktop Shell + Python Worker**, nicht Rust-Neubau.
- Python bleibt Owner für STT, Pipecat, Provider, Summaries, SQLite, Uploads, Exports und Jobs.
- Rust/Tauri übernimmt Shell, Fenster, Tray, Autostart, Worker-Supervision, Packaging, Signing, Update-Mechanik und später selektiv OS-nahe Funktionen.
- Umsetzung auf Big Branch, aber mit harten Gates: Wenn ein Gate scheitert, wird nicht weiter migriert, sondern stabilisiert.

## Architekturgrenzen

- **Nicht nach Rust portieren:** STT-Provider, Pipecat-Pipeline, Summaries, SQLite, ffmpeg/yt-dlp, Exporte.
- **Zunächst nicht nach Rust portieren:** Mikrofon-Capture. Python/sounddevice bleibt Default, Rust-Audio nur nach Messbeweis.
- **Nach Rust/Tauri verschieben:** Desktop-Shell, Prozessaufsicht, Tray, Hotkey, Autostart, Single Instance, Crash-/Log-Support.
- **API bleibt zuerst REST + WebSocket** über localhost. Kein neues IPC-Protokoll, bevor die Desktop-Shell stabil ist.
- **Security:** Tauri-Capabilities minimal halten; Sidecar-Ausführung nur für den Scriber-Worker erlauben.

## Phase 0: Baseline und Scope-Freeze

- Branch: `codex/hybrid-tauri-performance`.
- Baseline messen:
  - Cold start bis UI sichtbar.
  - Backend ready time.
  - Hotkey bis Recording-State.
  - Hotkey bis erster Audioframe.
  - Stop bis Text-Injection.
  - Upload/Export unter Last.
  - WebSocket-Events/sec und JSON-Serialize-Kosten.
  - History-Scroll mit vielen Transkripten.
- Offene P0-Performance-Fixes vorziehen oder explizit als Schuld markieren:
  - No-client WebSocket fast path.
  - O(n²)-Transcript-Append.
  - Upload/Export off-thread.
  - Settings-Debounce.
  - Frontend-History-Virtualisierung.
- Gate: Vorher/nachher-Messwerte existieren, sonst keine Bewertung der Tauri-Migration.

## Phase 1: Contract-First Boundary

- REST- und WebSocket-Payloads versionieren.
- Neue/erweiterte Endpunkte:
  - `GET /api/health`: readiness, apiVersion, workerVersion, pid, active session.
  - `GET /api/runtime`: capabilities, feature flags, runtime mode, dataDir.
  - `GET/POST /api/runtime/frontend-ready`: token-geschuetzter WebView-Ready-Beacon fuer installierte Frontend-Start-Evidence.
  - `POST /api/runtime/shutdown`: nur lokal und token-geschützt.
- Tauri startet Worker mit zufälligem Session-Token; UI sendet Token bei lokalen Requests.
- Frontend-API-Typen strikt halten, keine neuen `any`-Grenzen.
- Gate: Contract-Tests verhindern inkompatible Payload-Änderungen.

## Phase 2: Python Worker als Sidecar

- Python-Backend als eigenständigen Worker betreiben.
- Tauri startet, überwacht und beendet den Worker.
- Worker bindet nur an `127.0.0.1`, bevorzugt freier Port mit Lock.
- UI erhält Backend-URL zur Laufzeit, nicht hart über `localhost:5000`.
- Logging:
  - Rust-Shell-Log.
  - Python-Worker-Log.
  - Crash-Metadata.
  - Support-Bundle ohne API-Keys.
- Gate: App startet auf sauberem Windows ohne Node und ohne manuelles Python-Setup.

## Phase 3: React in Tauri, Express entkoppeln

- `Frontend/src-tauri/` ergänzen.
- Vite produziert statische Assets für Tauri.
- Express bleibt nur Dev-/Legacy-Pfad oder wird klar deprecated.
- `Frontend/client/src/lib/backend.ts` liest:
  - zuerst Tauri Runtime Backend URL,
  - danach `VITE_BACKEND_URL`,
  - danach Default `http://127.0.0.1:8765`.
- CORS für Browser-Dev und Tauri-Webview sauber trennen.
- Default-CORS muss Tauri-Produktions-Origins (`tauri.localhost` und `tauri://localhost`) akzeptieren; Tauri-CSP muss den IPC-Connect-Kanal erlauben.
- Installed-Smokes muessen nicht nur Backend/Static-Assets, sondern auch die echte Tauri-WebView pruefen: React muss `get_backend_access` nutzen, den Runtime-Backend-URL setzen und `/api/runtime/frontend-ready` mit Session-Token erreichen.
- Gate: Browser-Dev und Tauri-Prod funktionieren parallel.

## Phase 4: Desktop-Funktionen migrieren

- Reihenfolge:
  - Fenster/App-Menü.
  - Tray.
  - Single Instance.
  - Autostart.
  - Worker-Restart.
  - globaler Hotkey.
  - native Notifications optional.
- Recording-State bleibt ausschließlich im Python-Backend.
- Tauri-Hotkey ruft nur bestehende API-Endpunkte für Start/Stop.
- Tkinter/Legacy-Tray bleibt Fallback bis Tauri zwei Release-Kandidaten stabil ist.
- Gate: Keine doppelte Zustandslogik zwischen Rust und Python.

## Phase 5: Device-Events und Audio

- Rust Device Watcher Windows-first prüfen.
- Python `DeviceMonitor` bleibt Fallback.
- PortAudio-Refresh nie während aktivem Stream erzwingen.
- Polling nur als langes Sicherheitsnetz.
- Rust-Audio-Prototyp nur unter `SCRIBER_AUDIO_ENGINE=rust`.
- Default-Wechsel auf Rust-Audio nur wenn:
  - Hotkey-to-first-frame besser ist,
  - Device-Kompatibilität nicht schlechter wird,
  - keine ersten Wörter verloren gehen,
  - Python-Audio als Fallback bleibt.

## Phase 6: Packaging, Signing, Updates

- Windows-first Installer planen.
- Python Worker, ffmpeg/yt-dlp, native DLLs und optionale Modelle sauber bündeln.
- Datenverzeichnis festlegen für DB, Settings, `.env`, Logs, Downloads, Modelle.
- Bestehende lokale Daten migrieren, nicht überschreiben.
- Code Signing und später Updater mit Signatur/Rollback-Konzept einplanen.
- Gate: Frische Installation, Upgrade, Deinstallation und Worker-Crash sind getestet.

## Phase 7: Testing und Validierung

- Backend:
  - Focused und breite `pytest`-Suiten fuer Contracts, Lifecycle, Security,
    DeviceMonitor, Mic-Prewarm, Upload/Export, Jobs, Provider-Routing und
    Performance-Hot-Path-Gates.
  - Sidecar-Startup-Import-Preflight vor PyInstaller und als gefrorener
    `scriber-backend --runtime-import-check`.
- Frontend:
  - `npm run check` und `npm run build` fuer TypeScript/Vite.
  - `scripts/smoke_frontend_browser.py` fuer echte Browser-Routen gegen einen
    synthetischen Backend-Server.
  - `scripts/smoke_tauri_desktop.ps1 -VerifyFrontend` und
    `scripts/smoke_windows_installer.ps1 -VerifyFrontend` fuer installierte
    Frontend-Assets ueber den laufenden Backend-Static-Fallback plus
    Tauri-Origin-CORS auf `/api/health` und tokenisiertem `/api/runtime`
    sowie den echten Tauri-WebView-Ready-Beacon auf
    `/api/runtime/frontend-ready`.
- Installer/Desktop:
  - NSIS-Install, Sidecar-Start, Backend-Health, Frontend-Assets,
    Worker-Crash-Recovery, kontrollierter Worker-Shutdown,
    Startup-Timeout-Recovery, Default-Port-Konflikt, External-Backend-Attach,
    Legacy-Datenmigration, Upgrade-Datenerhalt, Support-Bundle-Redaktion,
    Hotkey-Registrierung, Stability-Proben und stille Deinstallation.
- Manuelle Windows-Hardware:
  - USB-Mic, Bluetooth-Mic, Dock an/ab, Windows-Default-Mic-Wechsel und
    Favorite-Mic-Fallback bleiben manuelle Gates mit
    `scripts/smoke_microphone_hardware_matrix.py`,
    `scripts/run_microphone_hardware_matrix.ps1` und
    `scripts/validate_microphone_hardware_matrix.py`.
- Gate:
  - Eine Aenderung gilt nur als abgeschlossen, wenn sie mit einem passenden
    automatisierten oder dokumentierten manuellen Gate in
    `docs/Hybrid-Architecture-Validation.md` belegt ist.

## Phase 8: Härtung und Cleanup

- Performance-Ziele:
  - UI P95 unter 3s sichtbar auf Referenzgerät.
  - Worker-ready P95 unter 5s ohne blockierendes Prewarm.
  - Idle CPU unter 1-2%.
  - 30-Minuten-Live-Session ohne Speicherwachstum durch Transcript-Strings.
  - Große Uploads/Exports blockieren `/api/health` und `/api/state` nicht.
- Legacy-Entrypoints erst entfernen, wenn Tauri Parität hat.
- README, AGENTS.md und Performance-Doku auf Tauri-first aktualisieren.
- Aktueller Stand:
  - Tauri-first Runtime, Sidecar-Packaging, NSIS-Installer, session-token
    geschuetzte Worker-API, Support-Bundle, Hotkey, Autostart, Single Instance,
    ffmpeg/ffprobe-Bundling, yt-dlp-Bundling, ONNXRuntime/Silero-VAD
    Runtime-Import-Gates, Updater-Plugin-Wiring,
    Authenticode-/Updater-Metadaten-Gates, Authenticode-Report-Output,
    Updater-Publikationsreport-Generator, SciPy-/ONNXRuntime-
    Footprint-Report, CI-Post-Publish-Updater-Verifikation, finaler
    Release-Readiness-Runner und installierte Smoke-Gates sind umgesetzt.
  - Python-Audio bleibt Default. `SCRIBER_AUDIO_ENGINE=rust` ist weiterhin nur
    ein requested Feature Flag, solange kein gemessener Rust-Audio-Prototyp
    existiert.
  - Idle-Mic-Prewarm existiert als Python/sounddevice App-Level-Manager und
    darf Backend-Readiness nicht blockieren.
- Nicht als abgeschlossen zaehlen, bis starke Evidence vorliegt:
  - reale Mic-Hardware-Matrix fuer USB/Bluetooth/Dock/Default-Wechsel,
  - reale Authenticode-Signatur mit erwartetem Publisher und optionalem
    Timestamp,
  - veroeffentlichtes signiertes Tauri-Updater-Manifest mit echten Signing Keys,
  - End-to-end Textinjektion in ein kontrolliertes Ziel mit persistiertem
    Zieltext, wenn dieser Nachweis fuer Release-Freigabe verlangt wird,
  - ein laengerer Live-Recording-Soak, falls ueber den vom Nutzer akzeptierten
    5-Minuten-Live-Gate hinaus ein strengeres Release-Kriterium festgelegt wird.

## Current Completion Audit

- Phase 0 ist fuer Startup, Backend-Readiness, Upload/Export-Last,
  WebSocket/JSON-Kosten, History-Scroll und Live-Hotpath inzwischen belegt.
  Am 2026-06-02 wurde nach der Always-On-Stream-Reuse-Aenderung in einem
  Tauri-managed Sidecar-Lauf mit migrierter Legacy-Konfiguration
  `hotkey_received_to_mic_ready_ms=70.933`,
  `hotkey_received_to_first_audio_frame_ms=99.428` und
  `stop_requested_to_first_paste_ms=1387.75` gemessen. Der kontrollierte
  Text-Target-Nachweis ist nun ebenfalls erbracht:
  `capturedSamples=1`, `maxCapturedChars=39`, `captureElapsedMs=4636.593`.
- Phase 1 bis 4 sind funktional weitgehend umgesetzt und durch Contract-,
  Security-, Supervisor- und installierte Desktop-Smoke-Gates belegt.
- Phase 5 ist nur fuer den Python-Audio-Pfad umgesetzt. Rust-Audio und ein
  Rust-Device-Watcher bleiben bewusst experimentell bzw. nicht default.
- Phase 6 ist fuer Standard-Cloud-Provider-Sidecar, NSIS, ffmpeg/ffprobe,
  yt-dlp, ONNXRuntime/Silero-VAD Startup-Imports, Runtime-Datenmigration,
  Release-Metadaten und optionale Gates umgesetzt. Am 2026-06-03 ist
  der aktuelle Standard-Installer nach der SciPy-Entfernung erneut per
  `scripts\build_windows.ps1 -RunMediaPreparationSmoke
  -RunRuntimeDependencyFootprint -RunInstallerFrontendSmoke
  -RunInstallerMediaPreparationSmoke -RunInstallerUninstallSmoke` mit
  kompletter Backend-Test-Suite, Frontend-Typecheck, Tauri-Release-Build,
  PyInstaller-Sidecar, ffmpeg/ffprobe-Bundling, Release-Smoke,
  Release-Metadaten, Updater-Metadatenvalidierung, Size-Report und
  installierten Frontend-/Media-/Uninstall-Smokes gebaut worden:
  `Scriber_0.1.0_x64-setup.exe`, `188.14 MiB`,
  SHA256 `b13d57f5cb6252bcf0eaa54db81bd67ffe96cdc5f1bbb1718bf7e8f29817ad22`;
  der Standard-Backend-Resource-Ordner liegt bei `523.01 MiB`, die
  temporaer installierte App bei `535.88 MiB`.
  In einem aelteren 2026-06-02-Standard-Build ist der Installer zusaetzlich mit Tauri-Origin-CORS fuer `/api/health`,
  tokenisiertem `/api/runtime`, echtem Tauri-WebView-Beacon ueber
  `/api/runtime/frontend-ready`, Cleanup, Uninstall, Support-Bundle,
  Crash-Recovery, kontrolliertem Worker-Shutdown, Startup-Timeout-Recovery,
  Default-Port-Konflikt, External-Backend-Attach und 205.78 MiB
  Setup-Artefakt unter dem 220 MiB Gate durchgelaufen. Echte Signierung und
  reale Updater-Veroeffentlichung sind externe Release-Schritte und noch nicht
  bewiesen; der Authenticode-Report-Output und der Report-Generator fuer den
  spaeteren Published-`latest.json`-Nachweis sind vorhanden. Der
  Authenticode-Report wird als UTF-8 ohne BOM geschrieben. Der Tag-
  Release-Workflow fuehrt den Published-Check nach der GitHub-Release-
  Veroeffentlichung aus, sobald Updater-Signing konfiguriert ist; dieser
  Nachweis verlangt inzwischen, dass auch die finale URL nach Redirects
  absolute HTTPS bleibt. Der
  finale Release-Readiness-Runner kann die Hardware-Matrix, Authenticode,
  Updater-Publikation und den finalen Aggregat-Check in einem Operator-Lauf
  zusammenfuehren, sobald die externen Nachweise existieren. Sein
  `-PlanOnly`-Modus schreibt inzwischen neben den konkreten Befehlen auch eine
  strukturierte `requiredEvidence`-Checkliste fuer physische Mic-Matrix,
  signierte Updater-Metadaten, Media-Preparation-Smoke, Runtime-Dependency-
  Footprint, veroeffentlichtes Updater-Manifest, Authenticode-Report und
  finalen Aggregat-Check. Tauri- und Installer-Smoke-`-OutputPath`-Artefakte
  werden ebenfalls als UTF-8 ohne BOM geschrieben. Der finale Aggregat-
  Validator wertet inzwischen auch `release-metadata\media-preparation-smoke.json`
  und `release-metadata\runtime-dependency-footprint.json` aus; der Windows-
  Release-Workflow erzeugt diese Artefakte im Standard-Release-Build mit
  `scripts\build_windows.ps1 -RunMediaPreparationSmoke -RunRuntimeDependencyFootprint`,
  und der finale Runner kann sie selbst erzeugen oder ueber
  `-UseExistingMediaPreparationReport` bzw.
  `-UseExistingRuntimeDependencyFootprintReport` wiederverwenden. Der
  Installer-Smoke kann denselben Media-Preparation-Gate inzwischen auch gegen
  die tatsaechlich installierten `backend\tools\ffmpeg`-Binaries ausfuehren:
  `scripts\smoke_windows_installer.ps1 -VerifyMediaPreparation` bzw.
  `scripts\build_windows.ps1 -RunInstallerMediaPreparationSmoke`.
- Phase 7 hat automatisierte Smoke-/Regression-Gates, aber die manuelle
  Hardware-Matrix bleibt offen. Einzel-Smoke, gefuehrter Windows-Runner und
  Aggregat-Validator fuer diese Matrix sind vorhanden; die physischen USB-,
  Bluetooth-, Dock-, Default-Mic- und Favorite-Fallback-Laeufe selbst sind noch
  nicht erbracht. Der Runner-Plan meldet inzwischen `readyForPhysicalRun` und
  `missingLabelParameters`, damit fehlende USB-/Dock-/Bluetooth-/Favorite-
  Labels vor den physischen Aktionen auffallen. Fuer Installer-Groesse ist
  `-ValidateSlimMediaTools` als expliziter Gate fuer kleinere FFmpeg-Kandidaten
  vorhanden. `scripts\smoke_media_preparation.py` prueft inzwischen die echten
  FFmpeg-basierten Python-Helfer fuer Datei-Kompression,
  Upload-Audioextraktion, YouTube-Post-Download-Normalisierung,
  Azure-MAI-MP3-Vorbereitung und optionale `ffprobe`-Dauerprobe; dieser Gate
  kann nun sowohl gegen den Release-Backend-Ordner als auch gegen eine
  temporaer installierte NSIS-App laufen. Status 2026-06-09: Gyan
  `release essentials` ist als Standard-Media-Tools-Quelle umgesetzt und wird
  mit `-ValidateSlimMediaTools` validiert; reale installierte
  YouTube-/Datei-/Azure-MAI-Media-Smokes bleiben fuer den naechsten
  vollstaendigen NSIS-Größenbeleg offen.
  Fuer SciPy/ONNXRuntime ist `scripts\analyze_backend_runtime_dependencies.py`
  vorhanden. Der aktuelle Sidecar-Snapshot meldet `33.75 MiB` fuer diese
  Runtime-Gruppe (`0.00 MiB` SciPy/SciPy libs, `33.75 MiB` ONNXRuntime), weil
  das lokale `pyloudnorm`-Kompatibilitaetsmodul Pipecat-Loudness ohne SciPy
  abdeckt. Der Gate erwartet SciPy als absent, prueft ONNXRuntime/Silero-VAD
  Pflichtpfade und lehnt ONNXRuntime-Beispiel-/Tooldaten ab.
- Phase 8 hat synthetische Guards, eine 30-Minuten-Idle-Stability, mehrere
  installierte Stability-Smokes und einen vom Nutzer fuer diese Iteration
  akzeptierten 5-Minuten-Live-Recording-Gate. Nicht erbracht sind reale
  Authenticode-/Updater-Publication, physische Hardware-Matrix und ein optional
  strengerer 30-Minuten-Live-Soak.
- Legacy-Tk/Tray ist entschieden: `docs/Legacy-Desktop-Fallback-Decision.md`
  haelt Tauri als primaeren Desktop-Pfad fest, belaesst Tkinter/Python-Tray aber
  als maintenance-only Fallback bis mindestens zwei stabile Tauri-Release-
  Kandidaten plus reale Hardware-/Release-Gates vorliegen.

## Testplan

- Backend: `pytest`, Contract-Tests, WebSocket no-client, Transcript-Buffer, Upload/Export off-thread, Settings-Debounce.
- Frontend: `npm run check`, `npm run build`, History-Scrolltest, Runtime-Backend-URL.
- Tauri/Rust: `cargo test`, Supervisor-Tests für freie/belegte Ports, Sidecar start/stop/restart.
- Manuell Windows: USB-Mic, Bluetooth-Mic, Dock an/ab, Default-Mic-Wechsel, Worker-Crash, Offline-Start, Upgrade alter Daten.

## Rust-Regel

Rust wird nur eingesetzt, wenn alle drei Punkte erfüllt sind:

1. Messung zeigt Python oder die aktuelle Desktop-Schicht als Engpass.
2. Die Rust-Komponente hat engeren Scope als das bestehende Python-Modul.
3. Python bleibt mindestens einen Release-Zyklus als Fallback erhalten.

## Quellen

- Tauri Architektur: [tauri.app architecture](https://v2.tauri.app/concept/architecture/)
- Tauri Sidecars: [embedding external binaries](https://v2.tauri.app/develop/sidecar/)
- Tauri Capabilities: [security capabilities](https://v2.tauri.app/security/capabilities/)
