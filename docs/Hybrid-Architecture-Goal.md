# Scriber Hybrid-Architektur Goal

Last updated: 2026-06-01

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

## Phase 7: Härtung und Cleanup

- Performance-Ziele:
  - UI P95 unter 3s sichtbar auf Referenzgerät.
  - Worker-ready P95 unter 5s ohne blockierendes Prewarm.
  - Idle CPU unter 1-2%.
  - 30-Minuten-Live-Session ohne Speicherwachstum durch Transcript-Strings.
  - Große Uploads/Exports blockieren `/api/health` und `/api/state` nicht.
- Legacy-Entrypoints erst entfernen, wenn Tauri Parität hat.
- README, AGENTS.md und Performance-Doku auf Tauri-first aktualisieren.

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
