# Windows Installer und Auto-Update Strategie fuer Scriber

## Ziel
Nicht versierte Nutzer sollen Scriber wie eine normale Windows-App installieren und automatisch auf dem aktuellen Stand bleiben, ohne Python, Node oder manuelle Update-Schritte.

## Empfohlene Zielvariante (optimal fuer Scriber jetzt)
### Primaerer Kanal
`Tauri Desktop Shell` + `Python Worker/Sidecar` + `NSIS Installer` + `GitHub Releases` + `Authenticode Signierung`

### Warum diese Variante
1. Passt zur aktuellen Python-Architektur ohne Re-Write: Rust/Tauri uebernimmt Shell/Lifecycle, Python bleibt STT-/Fachlogik.
2. Schnell produktionsfaehig im Vergleich zu MSIX-only oder Squirrel-Integration.
3. Silent Updates sind robust moeglich; aktuell ueber Tauri/NSIS, spaeter bei Bedarf mit eigenem Updater/Launcher.
4. Security kann sauber gehaertet werden (SHA256, Signaturpruefung, immutable Releases).
5. Der aktuelle Code enthaelt bereits Tauri 2 mit Rust-Supervisor, Windows-Single-Instance-Mutex, PyInstaller-Sidecar, NSIS-Build, installiertem Smoke-Test, Tauri-Updater-Plugin-Wiring und Manifest-Gates; offen bleiben echte Signier-Keys, signierte Release-Artefakte und die veroeffentlichte Update-Manifest-Aktivierung.

### Warum nicht Electron/MSIX-only/Squirrel jetzt
| Alternative        | Warum nicht jetzt                                                                       |
|--------------------|----------------------------------------------------------------------------------------|
| Electron           | Deutlich groesserer Runtime-Footprint, kein Vorteil fuer die vorhandene Python-STT-Domaene. |
| Tauri ohne Python-Sidecar | Nur sinnvoll, wenn STT-/Audio-/Providerlogik neu geschrieben wuerde. Der aktuelle Hybrid-Ansatz vermeidet diesen Rewrite. |
| MSIX + AppInstaller| Erfordert saubere MSIX-Paketierung + Signierung; Update-Settings sind je nach Windows-Version unterschiedlich verfuegbar (Basis seit Windows 10, Version 1709). |
| Squirrel.Windows   | Auch fuer Nicht-.NET-Apps nutzbar, aber NuGet/Squirrel-Artefaktmodell passt nicht direkt zur aktuellen Python-Build-Pipeline und erhoeht den Integrationsaufwand. |
| PyUpdater / Esky   | Nicht mehr aktiv gepflegt (Stand 2026). Eigener Updater ist transparenter und robuster. |

### Zielbild in einem Satz
Scriber laeuft als signierte, installierte Windows-App (per-user), startet single-instance, fuehrt eine lokale Python-Worker-Komponente unter Tauri-Supervision aus, schuetzt den lokalen Worker per Session-Token, prueft im Hintergrund Releases, laedt signierte Updates, installiert sie still im Leerlauf und startet sich neu.

---

## Architektur der Update-Loesung

### 1) Packaging Runtime (ohne Dev-Toolchain beim Nutzer)
1. Backend + Tray als `frozen` Python-App via **PyInstaller `onedir`-Modus**.
   - `onedir` ist Pflicht: deutlich schnellerer Start als `onefile` (kein Temp-Entpacken), Antivirus-freundlicher, partiell updatebar.
   - `onefile` ist **nicht geeignet**: langsamer Start (mehrere Sekunden Entpacken), Antivirus-Scans bei jedem Start, keine Delta-Updates moeglich.
2. Frontend als **statische Build-Artefakte** (`Frontend/dist/public`).
   - Aktuell laeuft Vite als Dev-Server zur Laufzeit (`npm run dev:client` via `tray.py:start_frontend()`). Das muss fuer die Produktionsversion ersetzt werden.
   - Build-Output via `npm run build` erzeugt statische Dateien in `Frontend/dist/public/`.
   - Backend (`web_api.py`) muss die statischen Dateien als Fallback routen (aiohttp `StaticResource` oder `FileResponse`).
   - **Kein Node.js beim Endnutzer noetig.**
3. FFmpeg als gebuendelte Dependency.
   - Status 2026-06-01: `src/runtime/media_tools.py` kapselt `ffmpeg`, `ffprobe` und `yt-dlp` Resolution fuer Dev-Modus, Env-Overrides und gebuendelte Sidecar-Tools.
   - Status 2026-06-01: `scripts/build_tauri_backend_sidecar.ps1 -BundleMediaTools` kopiert lokale `ffmpeg`/`ffprobe` Binaries nach `tools\ffmpeg\` im Sidecar.
   - Status 2026-06-01: `packaging/scriber-backend.spec` buendelt das `yt-dlp` Python-Paket fuer den Standard-Sidecar.

### 2) Installer
1. **Inno Setup**, per-user Install nach `%LocalAppData%\Scriber`.
   - Per-user vermeidet UAC-Prompts und erfordert keine Admin-Rechte.
   - Stabiler `AppId` im Format `{GUID}` fuer saubere Upgrade-Erkennung.
   - Status 2026-06-01: Der produktive Installer-Pfad nutzt aktuell Tauri/NSIS statt Inno Setup. `Frontend/src-tauri/tauri.conf.json` setzt `bundle.active=true`, `targets=["nsis"]`, `installMode="currentUser"` und mappt den Backend-Sidecar als Resource nach `backend/`.
   - Status 2026-06-01: `scripts/build_windows.ps1` erzeugt den NSIS-Build ueber `npm run tauri:build -- --bundles nsis` und laesst Tauri vorher den Sidecar bauen/kopieren.
   - Status 2026-06-01: `scripts/smoke_windows_installer.ps1` installiert das erzeugte Setup temporaer, startet die installierte App ohne Dev-Fallback, verifiziert `tauri-supervised` Sidecar-Start und entfernt Testinstallation/Testdaten wieder. Mit `-SimulateBackendCrash` toetet der Smoke den Worker, wartet auf den Tauri/Frontend-Recovery-Pfad und prueft `backend-crash-metadata.jsonl`. Mit `-OccupyDefaultPort` belegt er `127.0.0.1:8765` vor App-Start und prueft die dynamische Backend-Portwahl. Mit `-SimulateBackendShutdown` ruft er den token-geschuetzten `/api/runtime/shutdown`-Endpunkt auf und prueft kontrolliertes Worker-Beenden plus Supervisor-Recovery. Mit `-AttachExternalBackend` startet er ein externes Python-Backend und prueft, dass die installierte Tauri-Shell andockt, ohne einen Sidecar zu starten. Mit `-SimulateBackendStartupTimeout` blockiert er den ersten Worker-Start vor Readiness und prueft Supervisor-Ersatzstart. Mit `-StabilityDurationSec <Sekunden>` prueft er wiederholte Health-/State-Probes, stabile Backend-PID und Backend-Working-Set-Samples vor dem Cleanup. Mit `-LegacyDataDir <alter Scriber-Pfad> -VerifyLegacyDataMigration -SimulateUpgrade` prueft er Legacy-Datenmigration und Datenerhalt ueber einen zweiten Installerlauf.
2. **Startmenue-Eintrag**, optional **Autostart** via Registry `HKCU\Software\Microsoft\Windows\CurrentVersion\Run`.
   - Status 2026-06-01: In Tauri-Desktop-Runtime wird Autostart von Rust-Commands (`get_desktop_autostart`, `set_desktop_autostart`) verwaltet; Browser/Legacy nutzt weiter `web_api.py` (`GET/POST /api/autostart`).
   - Status 2026-06-01: Tauri schreibt `HKCU\Software\Microsoft\Windows\CurrentVersion\Run\Scriber` auf die aktuelle Desktop-Exe und behandelt alte Python-Tray-Kommandos als deaktiviert, damit der Tauri-Pfad nicht versehentlich Legacy-Tray startet.
3. **Uninstall** ohne Loeschen von Nutzerdaten.
   - Folgende Dateien/Ordner muessen in `[UninstallDelete]` **ausgeschlossen** werden:
     - `transcripts.db` (+ WAL/SHM-Dateien)
     - `settings.json`
     - `.env`
     - `downloads/` (heruntergeladene YouTube-Audio-Dateien)
      - `models/` (lokale Modell-Caches)
   - Inno Setup: `[InstallDelete]` fuer alte Versionsdateien nutzen, aber Nutzerdaten unangetastet lassen.
   - Status 2026-06-01: `.env`, `settings.json`, `transcripts.db`, Downloads und Modelle werden im Tauri/Python-Pfad unter `SCRIBER_DATA_DIR` gefuehrt. Beim ersten Start mit User-Data-Verzeichnis kopiert `src.runtime.paths.migrate_legacy_runtime_data()` fehlende Dateien aus `SCRIBER_LEGACY_DATA_DIR` oder typischen Source-Checkout-Pfaden wie `Documents\Github\Scriber`, ohne vorhandene Zieldaten zu ueberschreiben. Der Desktop-/Installer-Smoke kann diese Migration mit echten Legacy-Daten verifizieren; `.env` und `settings.json` werden per Hash verglichen, die SQLite-Datei wird wegen laufender Backend-Locks auf Existenz/Groesse geprueft.
4. **CloseApplications-Unterstuetzung**: Inno Setup `CloseApplications=yes` damit laufende Scriber-Instanzen vor dem Upgrade sauber heruntergefahren werden.
   - Voraussetzung: Die Scriber-App muss eine `AppMutex`/Named Mutex registrieren, die der Installer oder Updater als laufende Instanz erkennen kann.
   - Status 2026-06-01: Die Tauri-Shell registriert vor dem Backend-Start den Windows-Named-Mutex `Local\ScriberDesktopSingleInstance`; eine zweite Desktop-Instanz beendet sich dadurch frueh, ohne einen zweiten Worker zu starten.
   - Status 2026-06-01: Die Tauri-Shell fuehrt einen eigenen Backend-Supervisor-Loop aus. Wenn der gemanagte Worker stirbt, schreibt Rust `backend-crash-metadata.jsonl` und startet einen Ersatz-Worker, ohne auf den React-Health-Poll angewiesen zu sein. `scripts/smoke_tauri_desktop.ps1 -SimulateBackendCrash` und der Installer-Smoke mit derselben Option pruefen diesen Pfad.

### 3) Updater-Komponente
Status 2026-06-01: Fuer den Tauri-Desktop-Pfad ist der primaere Updater jetzt der Tauri-v2-Updater, nicht ein Python-Downloader. `Frontend/src-tauri/src/lib.rs` initialisiert `tauri-plugin-updater` und `tauri-plugin-process`, `Frontend/client/src/lib/desktop-updates.ts` stellt den Settings-Check/Install-Pfad bereit, und `scripts/build_windows.ps1 -EnableTauriUpdater` aktiviert signierte Tauri-Updater-Artefakte nur, wenn Public-Key, Endpoint und Tauri-Signing-Key vorhanden sind. Ohne diese Konfiguration bleibt die UI absichtlich inaktiv bzw. meldet "not configured".

Frueherer Python-Plan, falls spaeter ein eigener Updater ausserhalb des Tauri-Plugins noetig wird:
Neue Komponente `src/updater.py`:
1. **Version Check** ueber GitHub Releases API (`https://api.github.com/repos/MyButtermilk/Scriber/releases/latest`) oder eigenes `latest.json` (gehostet als Release-Asset oder via GitHub Pages).
   - GitHub API hat Rate-Limits (60 Anfragen/h ohne Token). Fuer moderate Nutzerzahlen kein Problem.
   - `releases/latest` liefert das aktuelle non-draft/non-prerelease Release; fuer deterministic Channels (`stable`, `beta`) ist ein eigenes `latest.json` robuster.
   - Empfehlung: `latest.json` als Release-Asset bevorzugen, da es Caching erlaubt und unabhaengig von API-Limits ist.
2. **Download** in `%LocalAppData%\Scriber\updates\`.
   - Atomarer Download: Erst in temporaere Datei schreiben, dann umbenennen.
   - Download-Fortschritt ueber WebSocket-Event an Frontend melden.
3. **Verifikation** (dreistufig, alle Pruefungen muessen bestanden werden):
   - **SHA256** gegen Manifest (`SHA256SUMS.txt` oder `latest.json`-Feld).
   - **Authenticode Signatur** via PowerShell `Get-AuthenticodeSignature` (Status `Valid` + erwarteter Publisher-Name).
   - **Version** des heruntergeladenen Installers muss neuer sein als die installierte Version (Downgrade-Schutz).
4. **Install-Trigger** nur wenn App im sicheren Zustand ist:
   - Kein aktives Recording (`is_listening == False` via `/api/state`).
   - Keine aktive Verarbeitung (kein `processing`-Status, keine Background-Jobs).
   - Kein laufender File-Upload / YouTube-Download.
   - Optional: Benutzerbestaetigung im Frontend ("Update verfuegbar. Jetzt neu starten?").
5. **Silent Installer** ausfuehren und danach kontrollierter Restart:
   - Ablauf: `tray.py` stoppt Backend + Frontend → startet Installer mit `/VERYSILENT /SUPPRESSMSGBOXES /NORESTART /SP- /CLOSEAPPLICATIONS` → Installer ueberschreibt Dateien → `tray.py` startet sich selbst neu (via Inno Setup `[Run]`-Section oder eigener Restart-Logik).
   - **Race-Condition vermeiden**: Die laufende `Scriber.exe` kann sich nicht selbst ueberschreiben. Loesung: Inno Setup mit `CloseApplications=yes` oder: Updater startet Installer, dann beendet sich selbst (Exit) → Installer ueberschreibt → startet neue Version via `[Run]`.

### 4) Launcher-Konzept (empfohlen)
Separater kleiner `Scriber-Launcher.exe` als Einstiegspunkt:
1. Prueft beim Start, ob ein heruntergeladenes Update in `updates/` bereitliegt (bereits verifiziert).
2. Falls ja: Fuehrt Installer aus, wartet auf Abschluss, startet dann neue Version.
3. Falls nein: Startet direkt die Haupt-App (`Scriber.exe`).
4. **Vorteil**: Umgeht das Problem, dass sich die laufende EXE nicht selbst ersetzen kann. Der Launcher bleibt stabil zwischen Versionen und ist so klein, dass er selten aktualisiert werden muss.

### 5) Release-Artefakte
Pro Release:
1. `Scriber-Setup-x64.exe` (signiert)
2. `Scriber-portable-x64.zip` (optional, fuer Power-User)
3. `SHA256SUMS.txt` (signiert oder im Release-Body)
4. `latest.json` (als Release-Asset)

Status 2026-06-01: `scripts/create_release_metadata.py` erzeugt `SHA256SUMS.txt` und `latest.json` fuer die gebauten NSIS-Artefakte. `scripts/build_windows.ps1` ruft das Script nach erfolgreichem Bundle-Build automatisch auf. `scripts/validate_tauri_updater_metadata.py` prueft das Manifest gegen das Tauri-Updater-Schema und erzwingt bei updater-aktivierten Builds nicht-leere Signaturen.

Aktuelles `latest.json`-Format:
```json
{
  "version": "0.1.0",
  "notes": "",
  "pub_date": "2026-06-01T13:00:00Z",
  "platforms": {
    "windows-x86_64": {
      "signature": "",
      "url": "https://github.com/MyButtermilk/Scriber/releases/download/v0.1.0/Scriber_0.1.0_x64-setup.exe"
    }
  },
  "artifacts": [
    {
      "name": "Scriber_0.1.0_x64-setup.exe",
      "url": "https://github.com/MyButtermilk/Scriber/releases/download/v0.1.0/Scriber_0.1.0_x64-setup.exe",
      "sha256": "a1b2c3d4e5f6...",
      "sizeBytes": 205000000,
      "signature": ""
    }
  ]
}
```

Felder gegenueber Tauri-Updater-Minimum ergaenzt:
- `artifacts[].sha256`: Ermoeglicht zusaetzliche Integritaetspruefung und manuellen Release-Asset-Abgleich.
- `artifacts[].sizeBytes`: Ermoeglicht Speicherplatzpruefung und Fortschrittsanzeige vor dem Download.
- `artifacts[].signature`: Wird leer geschrieben, bis Updater-Signierung konfiguriert ist; spaeter Inhalt der `.sig`-Datei.

---

## Installationsgroesse klein halten (konkrete Strategie)

### A) Produkt-Schnitt: `Lite` als Default, `Full/Offline` optional
1. **Default-Installer = Lite**:
   - Cloud-STT + Web-Features enthalten.
   - Schwere lokale ASR-Stacks (`torch`, `nemo_toolkit`, grosse ONNX/Nemo-Modelle) **nicht** im Standard-Installer.
2. **Optionaler Zusatzkanal = Full/Offline Pack**:
   - Separate Artefakte wie `Scriber-Offline-Pack-x64.zip` oder eigener Full-Installer.
   - Download nur bei Bedarf aus Settings ("Offline-Spracherkennung installieren").
3. **Vorteil**:
   - Kleiner Erst-Download fuer die Mehrheit.
   - Power-User bekommen weiterhin lokale/offline Features.

### B) Python-Dependencies in Build-Profile aufteilen
1. Status 2026-06-01: `requirements.txt` ist ein Aggregator aus:
   - `requirements-base.txt` (Runtime fuer Standardnutzer),
   - `requirements-local-asr.txt` (nur lokale ASR-Provider),
   - `requirements-dev.txt` (Tests/Tools).
2. Status 2026-06-01: `requirements-base.txt` enthaelt explizit `scipy`, weil Pipecat ueber `pyloudnorm` beim Backend-Start davon abhaengt.
3. Status 2026-06-01: Der GitHub Release-Build nutzt `requirements-base.txt`, `requirements-dev.txt` und `requirements-build.txt`, aber nicht `requirements-local-asr.txt`.
4. Lokale Provider werden per **Lazy Import** geladen (erst wenn Nutzer sie aktiviert).
5. Nicht genutzte Provider im Standard-Build per Feature-Flag deaktivieren.

### C) PyInstaller gezielt schlank halten
1. In `scriber.spec` nur benoetigte `hiddenimports`/`datas` aufnehmen.
   - Status 2026-06-01: `packaging/scriber-backend.spec` listet SciPy/pyloudnorm explizit fuer den Pipecat-Startup-Pfad und schliesst schwere lokale ASR-Stacks aus.
   - Status 2026-06-01: `scripts/check_backend_runtime_imports.py` prueft kritische Startup-Imports vor PyInstaller; danach startet der Build den gefrorenen `scriber-backend --runtime-import-check`, damit fehlende Module wie SciPy den Build stoppen statt erst beim Endnutzer zu crashen.
2. Unnoetige Inhalte explizit ausschliessen:
   - `tests`, `__pycache__`, Dokumentation, Beispiele, nicht benoetigte Provider-Assets.
3. Source Maps und Debug-Artefakte fuer Release-Build deaktivieren (Frontend + Python-Pakete soweit moeglich).
4. Optionaler `UPX`-Testpfad nur als A/B-Option (manche AV-Engines reagieren empfindlich).

### D) Medien- und Modell-Binaries entkoppeln
1. **FFmpeg-Strategie**:
   - Entweder schlanke mitgelieferte Binary (nur benoetigte Codecs),
   - oder "on-demand Download" beim ersten Datei/YouTube-Transkript.
2. **Modelle nie in den Core-Installer einbetten**:
   - Download in `%LocalAppData%\\Scriber\\models\\` nach Nutzerentscheidung.
3. Modelle versioniert halten, damit sie getrennt vom App-Update aktualisiert werden koennen.

### E) CI-Groessenbudget als harter Gate
1. Build-Pipeline schreibt `size-report.json` mit:
   - Installer-Groesse,
   - installierte Groesse (`dist/app`),
   - groesste 20 Dateien.
2. CI failt bei Grenzwert-Ueberschreitung (Startwerte):
   - `Lite Setup <= 220 MB`,
   - `installierte Lite-App <= 450 MB`.
3. Jede Release-PR enthaelt den Groessenvergleich zur Vorversion.

### F) Update-Bandbreite minimieren (ab Phase 2)
1. Zunaechst Full-Installer beibehalten (einfacher und robust).
2. Danach optional Dateiebene-Delta fuer grosse Bloecke (`models`, grosse DLL-Sets) evaluieren.
3. Unveraenderte grosse Komponenten in eigene Pakete auslagern, damit App-Updates klein bleiben.

---

## Konkreter Umsetzungsplan

### Phase 0 - Grundlagen (Pflicht)
1. `LICENSE`-Datei im Repo ergaenzen (MIT konsistent zu README).
   - Status 2026-06-01: Root-`LICENSE` existiert als MIT-Lizenzdatei.
2. **Verbindliche Versionierung** einfuehren:
   - Status 2026-06-01: `src/version.py` existiert mit `__version__`, SemVer-Normalisierung und `SCRIBER_VERSION` Runtime-Override.
   - Status 2026-06-01: `web_api.py` meldet `version` in `/api/runtime` und `/api/health`.
   - Status 2026-06-01: `scripts/sync_version.py` synchronisiert `src/version.py` nach `tauri.conf.json`, `Cargo.toml`, `Frontend/package.json` und `Frontend/package-lock.json`.
   - Status 2026-06-01: `build_windows.ps1` fuehrt die Version-Synchronisierung vor Tests/Build automatisch aus.
3. Zielplattform fixieren: **Windows 10/11 x64** (Build 1809+).
4. Install Scope festlegen: **per-user** (empfohlen, kein UAC).
5. **Frozen-Mode-Erkennung** einfuehren:
   - Status 2026-06-01: `src/runtime/paths.py` existiert fuer `is_frozen()`, `SCRIBER_DATA_DIR`, `settings.json`, `transcripts.db` und Downloads.
   - Noch offen: Pfad-Resolution fuer gebuendelte Assets und Frontend-Static-Files (`sys._MEIPASS`/Tauri Resources).
   - Betrifft weiterhin: `tray.py` (Icon-Lade-Pfad), `web_api.py` (Static-File-Serving).

### Phase 1 - Produktisierbarer Build
1. **Tauri Backend-Sidecar bereitstellen**:
   - Status 2026-06-01: `src/backend_worker.py` existiert als eigenstaendiger Worker-Entry-Point.
   - Status 2026-06-01: `packaging/scriber-backend.spec` und `scripts/build_tauri_backend_sidecar.ps1` bauen einen PyInstaller-`onedir`-Sidecar.
   - Status 2026-06-01: Der Rust-Supervisor bevorzugt `SCRIBER_BACKEND_EXE` nur fuer erlaubte `scriber-backend`-Dateinamen bzw. `backend\scriber-backend.exe` neben der Tauri-Exe und faellt im Dev-Modus auf `python -m src.web_api` zurueck.
   - Status 2026-06-01: Der Standard-Sidecar ist ein Cloud-Provider/Lite-Build und schliesst schwere lokale ASR-Stacks (`torch`, NeMo, ONNX-ASR) aus.
   - Status 2026-06-01: Tauri bundelt `target/release/backend/` als Resource `backend/`, sodass installierte NSIS-Builds denselben Sidecar-Pfad nutzen.
   - Status 2026-06-01: Der Sidecar-Build fuehrt vor PyInstaller einen Runtime-Import-Preflight aus und prueft danach den gefrorenen Sidecar mit `--runtime-import-check`; beides deckt unter anderem SciPy, pyloudnorm, Pipecat und `src.web_api` ab.
   - Status 2026-06-01: Die Default-Capability ist auf App-Version, Prozess-Relaunch und Updater-Check/Download-Install beschraenkt; Tauri-Shell- und Opener-Plugin sind nicht registriert. `tests/test_tauri_security_gates.py` prueft diese Grenze.
   - Status 2026-06-01: Der Tauri-Supervisor erzeugt ein zufaelliges `SCRIBER_SESSION_TOKEN`, uebergibt es an den Python-Worker und stellt es dem React-Frontend ueber `get_backend_access` bereit. Das Backend erzwingt den Token fuer lokale REST-/WebSocket-Zugriffe; `/api/health` bleibt fuer Readiness tokenfrei.
   - Status 2026-06-01: Die Windows-Tauri-Shell erzwingt Single-Instance-Start ueber den Named Mutex `Local\ScriberDesktopSingleInstance`, bevor der Backend-Supervisor einen Worker starten kann.
   - Status 2026-06-01: Windows-Autostart ist im Tauri-Pfad implementiert; `Frontend/client/src/lib/backend.ts` routet Settings-Autostart-Aufrufe in Desktop-Runtime auf Rust statt auf den Python-Endpoint.
   - Status 2026-06-01: Tauri-App-Menue und Tauri-Tray sind fuer Shell-/Lifecycle-Aktionen implementiert: Hauptfenster oeffnen/fokussieren, managed Backend neu starten, App beenden.
   - Status 2026-06-01: Globaler Hotkey ist im Tauri-Pfad implementiert; Rust registriert den in `/api/settings` konfigurierten Shortcut, deaktiviert Python-Keyboard-Hooks fuer managed Worker und ruft nur die bestehenden Live-Mic-Endpunkte auf.
   - Status 2026-06-01: `POST /api/runtime/shutdown` existiert als loopback- und token-geschuetzter Shutdown-Endpunkt fuer kontrolliertes Worker-Beenden.
   - Status 2026-06-01: WebSocket-Events tragen `apiVersion` und werden ueber `src/core/ws_contracts.py` sowie `tests/contract/test_ws_events.py` gegen bekannte Eventtypen validiert. Das React-Frontend nutzt dafuer eine typisierte `ScriberWebSocketMessage`-Union.
   - Status 2026-06-01: Tauri schreibt Shell-Lifecycle-Logs und Backend-Exit-Metadaten unter `SCRIBER_DATA_DIR\logs\`; `POST /api/runtime/support-bundle` erzeugt ein redigiertes Diagnose-ZIP ohne API-Keys oder Session-Tokens.
2. **Frontend Production Build** in den Backend-Output integrieren:
   - `npm run build` erzeugt `Frontend/dist/public/` mit statischem HTML/JS/CSS.
   - `build_windows.ps1` kopiert diesen Output in das PyInstaller-Output-Verzeichnis.
   - `web_api.py` erhaelt einen Static-File-Handler, der im Frozen-Modus `dist/public/` als Root served.
3. **Tray/Backend Start umbauen**: `start_frontend()` in `tray.py` darf im Frozen-Modus **nicht** `npm run dev:client` starten.
   - Konditional: Wenn `is_frozen()`, dann kein Frontend-Prozess starten; die statischen Dateien werden direkt vom Backend geserved.
   - Falls nicht frozen (Dev-Modus): bestehendes Verhalten beibehalten.
4. **Freeze Build** erstellen:
   - PyInstaller `onedir`, Zielverzeichnis `dist/tauri-sidecar/scriber-backend/`.
   - Entry-Point: `src/backend_worker.py` (wird vom Tauri-Supervisor gemanagt).
   - Hidden imports explizit listen (basierend auf Codebase-Analyse):
     - `pipecat-ai` Submodule (google, assemblyai, silero, deepgram, openai, azure, gladia, groq, speechmatics, aws, elevenlabs)
     - `scipy`, `scipy.signal`, `pyloudnorm`
     - `sounddevice`, `pycaw`, `keyboard`, `pyautogui`
     - `PySide6`, `pystray`, `customtkinter`
     - `yt-dlp`, `python-docx`, `reportlab`, `lxml`
   - Daten-Dateien einschliessen: `src/assets/`, `Frontend/dist/public/`, optionale FFmpeg/FFprobe-Binaries ueber `-BundleMediaTools`.
5. **Reproduzierbarer Build** per Script.
   - Status 2026-06-01: `scripts/build_windows.ps1` orchestriert Version-Sync, Tests, Frontend-Typecheck, Tauri/NSIS-Build, Release-Metadaten und optionalen Release-/Installer-Smoke-Test.
6. **Size-Profiling direkt in Phase 1**:
   - Status 2026-06-01: `requirements-base.txt`, `requirements-local-asr.txt`, `requirements-dev.txt` und `requirements-build.txt` existieren.
   - Lite-Build als Standard ist im Sidecar-Spec vorgespurt; Gesamtpipeline in `build_windows.ps1` bleibt offen.
   - Groessenreport (`size-report.json`) erzeugen und in CI publizieren.

Lieferobjekte:
1. `scripts/build_tauri_backend_sidecar.ps1` (umgesetzt fuer Sidecar, Import-Preflight, gefrorenen Runtime-Import-Check, Frontend-Build, optionales FFmpeg/FFprobe-Bundling und Copy nach Tauri Release)
2. `packaging/scriber-backend.spec` (umgesetzt fuer Standard-Cloud-Sidecar inkl. `yt-dlp`, SciPy und pyloudnorm)
3. `src/version.py` (umgesetzt)
4. `src/runtime/paths.py` (teilweise umgesetzt: Runtime-Data-Pfade; Asset-Resolution offen)
5. `src/runtime/media_tools.py` (umgesetzt: zentrale Resolution fuer `ffmpeg`, `ffprobe`, `yt-dlp`)
6. `requirements-base.txt`, `requirements-local-asr.txt`, `requirements-dev.txt` (umgesetzt)
7. `size-report.json` (CI-Artefakt)
8. Start/Healthcheck fuer gebaute App (Release-Smoke vorhanden; Sidecar-Pfad optional ueber `-BackendExePath`)
9. `scripts/smoke_windows_installer.ps1` (umgesetzt: temporaere NSIS-Installation, Start ohne Dev-Fallback, Sidecar-Verifikation, optionale Worker-Crash-Recovery-, belegter-Default-Port-, kontrollierter-Shutdown-, External-Backend-Attach-, Startup-Timeout-, Stability-, Legacy-Datenmigrations- und Installer-Rerun-Datenerhalt-Checks, Cleanup)
10. `scripts/build_windows.ps1` (umgesetzt fuer Version-Sync, Tauri/NSIS-Release-Build, Release-Metadaten, Smoke-Test und optionale Worker-Crash-/Port-Konflikt-/kontrollierter-Shutdown-/External-Backend-Attach-/Startup-Timeout-/Stability-/Legacy-Daten-/Upgrade-Smokes; Signing/Updater offen)

### Phase 2 - Installer
1. `installer/scriber.iss` erstellen.
   - Kernkonfiguration:
     ```ini
     [Setup]
     AppId={{UNIQUE-GUID-HERE}
     AppName=Scriber
     AppVersion={#AppVersion}
     DefaultDirName={localappdata}\Scriber
     PrivilegesRequired=lowest
     OutputBaseFilename=Scriber-Setup-x64
     Compression=lzma2/ultra64
     SolidCompression=yes
     CloseApplications=yes
     AppMutex=Local\ScriberDesktopSingleInstance
     UninstallDisplayIcon={app}\Scriber.exe
     ```
   - `[Files]`: Alle Dateien aus `dist/app/`.
   - `[Icons]`: Startmenue-Link + optionaler Desktop-Link.
   - `[Run]`: App nach Installation starten (optional, `postinstall` Flag).
   - `[UninstallDelete]`: Gezielt nur App-Dateien, niemals `transcripts.db`, `settings.json`, `.env`, `downloads/`.
2. **Install/Upgrade/Uninstall** inkl. Datenerhalt testen:
   - Testmatrix: Frischinstallation, Upgrade mit laufender App, Upgrade mit Nutzerdaten, Deinstallation.
   - Status 2026-06-01: `scripts/smoke_windows_installer.ps1 -LegacyDataDir C:\Users\Alexander.Immler\Documents\Github\Scriber -VerifyLegacyDataMigration -SimulateUpgrade` validiert temporaere NSIS-Installation, erste Migration von `.env`, `settings.json` und `transcripts.db`, zweiten Installerlauf gegen dasselbe Installationsverzeichnis und Erhalt eines Daten-Sentinels im bestehenden `SCRIBER_DATA_DIR`.
3. **Silent Upgrade** lokal validieren:
   - `Scriber-Setup-x64.exe /VERYSILENT /SUPPRESSMSGBOXES /NORESTART /SP- /CLOSEAPPLICATIONS`

Lieferobjekte:
1. `installer/scriber.iss`
2. `Output/Scriber-Setup-x64.exe`
3. Install-Doku fuer Support

### Phase 3 - Auto-Update
1. `src/updater.py` implementieren:
   - `UpdateManager`-Klasse mit States: `idle` → `checking` → `downloading` → `verifying` → `ready` → `installing`.
   - Hintergrund-Thread/Task fuer periodischen Check (alle 4 Stunden, konfigurierbar).
   - Download mit Progress-Callback fuer WebSocket-Events.
   - SHA256 + Authenticode Verifikation.
   - Downgrade-Blockade via SemVer-Vergleich.
2. API-Hooks:
   - `GET /api/update/status` – Aktueller Update-Status, verfuegbare Version, Download-Fortschritt.
   - `POST /api/update/check` – Manuellen Check ausloesen.
   - `POST /api/update/install` – Installation des heruntergeladenen Updates ausloesen.
   - `POST /api/update/dismiss` – Update-Benachrichtigung ausblenden bis zum naechsten Release.
3. WebSocket Events:
   - `update_available` – Neue Version erkannt (`{version, release_notes, size}`).
   - `update_progress` – Download-Fortschritt (`{percent, downloaded_bytes, total_bytes}`).
   - `update_ready` – Download abgeschlossen und verifiziert.
   - `update_error` – Fehler mit Klartext-Beschreibung.
4. Tray-Menue (Erweiterung von `create_menu()` in `tray.py`):
   - "🔔 Update verfuegbar (v1.4.0)" (sichtbar nur wenn Update bereit).
   - "Check for updates" (manueller Trigger).
   - Badge/Dot-Indikator am Tray-Icon wenn Update verfuegbar.
5. Frontend Settings (Erweiterung von `Settings.tsx`):
   - Sektion "Updates":
     - Toggle: Auto-Check aktiv/inaktiv.
     - Channel: stable / beta.
     - Letzter Check: Zeitstempel + Ergebnis.
     - Aktuelle Version + verfuegbare Version.
     - Button: "Jetzt pruefen" / "Jetzt installieren".
     - Download-Fortschrittsbalken.
6. Config-Erweiterung (`config.py`):
   - `SCRIBER_AUTO_UPDATE=1` (Default: aktiv).
   - `SCRIBER_UPDATE_CHANNEL=stable` (stable/beta).
   - `SCRIBER_UPDATE_CHECK_INTERVAL_HOURS=4`.
   - `SCRIBER_UPDATE_URL` (Override fuer Custom-Server, Default: GitHub).

### Phase 4 - CI/CD + Signierung
1. **GitHub Action** fuer Tag-Releases (`v*`):
   - `.github/workflows/release-windows.yml`
   - Status 2026-06-01: Workflow existiert fuer `workflow_dispatch` und Push von Tags `v*`.
   - Status 2026-06-01: Runner ist `windows-latest` mit Python 3.13, Node 20 und Rust stable.
2. **Build-Pipeline**:
   - Status 2026-06-01: Checkout -> Python/Node/Rust Setup -> `pip install` fuer base/dev/build -> `npm ci` -> `scripts/build_windows.ps1 -SkipSmoke` -> NSIS-Artefakt + `latest.json` + `SHA256SUMS.txt` als Workflow-Artefakt.
   - Status 2026-06-01: Bei `v*` Tags publiziert `softprops/action-gh-release` die erzeugten Artefakte als GitHub Release.
   - Status 2026-06-01: Tauri-Updater-Plugin, Frontend-Check/Install-UI und Manifest-/Signing-Gates sind integriert. Ohne `SCRIBER_TAURI_UPDATER_PUBLIC_KEY` und `TAURI_SIGNING_PRIVATE_KEY` bleibt der Workflow beim bisherigen NSIS-Release ohne Updater-Artefakte.
   - Noch offen: Authenticode Signing in CI, echte Tauri-Updater-Keys, signierte Update-Artefakte und veroeffentlichtes `latest.json`.
3. **Signierung**:
   - Authenticode mit OV- oder EV-Zertifikat.
   - Public-Trust Code-Signing verlangt heute hardwaregeschuetzten Schluessel (Token oder HSM/Cloud-HSM, je nach Anbieter). Fuer CI/CD sind Cloud-Signing-Dienste wie **Microsoft Trusted Signing** praktikabel.
   - EV-Zertifikat kann den Erstvertrauen-Eindruck verbessern; SmartScreen bleibt jedoch reputationsbasiert und ist nicht allein durch EV garantiert.
   - Signierung in CI via `signtool.exe` oder Cloud-Signing-API.
   - Timestamp-Server einbinden (`/tr http://timestamp.digicert.com /td sha256`) damit Signatur auch nach Zertifikatsablauf gueltig bleibt.
4. **Release Immutability** einschalten (falls im Repo verfuegbar) und Release-Assets nach Publish nicht mehr austauschen.
5. **Versionierung automatisieren**: Tag-Name (`v1.4.0`) wird als `AppVersion` in Inno Setup und in `src/version.py` eingesetzt (via CI-Script).

---

## Security Baseline (nicht optional)
1. Jede EXE signieren (App + Installer) mit SHA256 + RFC 3161 Timestamp.
2. Updater installiert nur, wenn:
   - SHA256 stimmt,
   - Signatur gueltig ist (`Valid` Status via `Get-AuthenticodeSignature`),
   - Publisher erwartet ist (Thumbprint oder Subject-Name Abgleich),
   - Version neuer ist als installiert (SemVer-Vergleich).
3. **Keine Update-URL aus User-Input** uebernehmen. Die URL ist hardcoded oder ueber signierte Config gesetzt.
4. **Downgrade blockieren** (ausser expliziter Recovery-Mode via CLI-Flag).
5. **HTTPS-only** fuer alle Downloads. Kein HTTP-Fallback.
6. Timeouts, Retry mit exponentiellem Backoff, atomare Dateioperationen (Download → Temp → Rename).
7. **Cleanup**: Heruntergeladene Installer-Dateien nach erfolgreicher Installation loeschen.
8. **Rollback-Strategie**: Wenn die neue Version nach dem Start crasht (3x innerhalb von 60 Sekunden), automatisch auf vorherige Version zuruecksetzen. Dafuer: Vor dem Update die aktuelle Version als Backup sichern.

---

## UX Regeln fuer nicht versierte Nutzer
1. **Default**: Automatisch nach Updates pruefen (alle 4 Stunden, konfigurierbar).
2. **Update-Download** unauffaellig im Hintergrund.
3. **Installation** nur in Leerlauf oder mit kurzem **Non-Modal-Dialog**:
   - "Scriber v1.4.0 ist verfuegbar. Jetzt neu starten?"
   - Optionen: "Jetzt neu starten" / "Spaeter" / "Release Notes anzeigen".
4. **Klare Fehlertexte** fuer typische Szenarien:
   - Netzwerk nicht erreichbar: "Update-Check fehlgeschlagen. Pruefe deine Internetverbindung."
   - Signatur ungueltig: "Update konnte nicht verifiziert werden. Bitte lade es manuell von github.com/MyButtermilk/Scriber herunter."
   - Speicherplatz: "Nicht genuegend Speicherplatz fuer das Update (XX MB benoetigte)."
   - Download abgebrochen: "Download wird beim naechsten Check fortgesetzt."
5. **Immer manueller Trigger** verfuegbar: "Jetzt nach Updates suchen" (Settings + Tray-Menue).
6. **Keine erzwungenen Updates**: Der Nutzer kann Updates dauerhaft ignorieren. Nur bei kritischen Sicherheits-Updates: prominente Warnung.
7. **Transparente Versionsanzeige**: Aktuelle Version sichtbar im Tray-Tooltip, Settings und Health-Endpoint.

---

## Repo-Aenderungen (konkret)

### Neue Dateien
| Datei | Beschreibung |
|-------|-------------|
| `src/version.py` | Umgesetzt: zentrale Versionsnummer (`__version__`) und SemVer-Normalisierung. |
| `src/updater.py` | Zurueckgestellt fuer den Tauri-Pfad; primaer ist aktuell `tauri-plugin-updater`. Nur wieder aufnehmen, wenn ein eigener Python-Updater bewusst dem Tauri-Updater vorgezogen wird. |
| `src/runtime/paths.py` | Teilweise umgesetzt: Runtime-Data-Pfade fuer Settings, SQLite und Downloads. Frontend-Asset-Resolution offen. |
| `src/runtime/media_tools.py` | Umgesetzt: zentrale Resolution fuer `ffmpeg`, `ffprobe`, `yt-dlp` ueber Env, Sidecar-Tools und System-PATH. |
| `src/runtime/support_bundle.py` | Umgesetzt: redigiertes Support-ZIP mit Runtime-/State-Metadaten, Logs und redigierter Config/Env. |
| `src/backend_worker.py` | Umgesetzt: Tauri/PyInstaller Worker-Entry-Point. |
| `Frontend/src-tauri/src/lib.rs` | Umgesetzt: Rust-Supervisor, Session-Token-Bridge, Worker-Lifecycle, App-Menue/Tray fuer Shell-Aktionen, Windows-Named-Mutex fuer Single Instance, Windows-Autostart via HKCU Run-Key, globaler Hotkey via bestehende Live-Mic-API, Tauri-Updater- und Process-Plugin-Initialisierung. |
| `packaging/scriber-backend.spec` | Umgesetzt: PyInstaller-Spec fuer den Backend-Sidecar inkl. SciPy/pyloudnorm-Startup-Abhaengigkeiten. |
| `installer/scriber.iss` | Inno Setup Script. |
| `scripts/check_backend_runtime_imports.py` | Umgesetzt: Preflight fuer kritische Backend-Startup-Imports vor PyInstaller und als gefrorener Sidecar-Check via `--runtime-import-check`. |
| `scripts/build_tauri_backend_sidecar.ps1` | Umgesetzt: Import-Preflight -> Frontend Build -> PyInstaller Sidecar -> gefrorener Runtime-Import-Check -> optionales FFmpeg/FFprobe-Bundling -> optionaler Copy nach Tauri Release. |
| `scripts/build_windows.ps1` | Umgesetzt fuer Tests -> Tauri/NSIS Bundle -> Release-Metadaten -> Release-/Installer-Smoke-Test. `-RunInstallerCrashSmoke` fuehrt den installierten Worker-Crash-Recovery-Gate aus; `-RunInstallerPortConflictSmoke` prueft dynamische Backend-Portwahl bei belegtem `127.0.0.1:8765`; `-RunInstallerControlledShutdownSmoke` prueft den token-geschuetzten kontrollierten Worker-Shutdown mit Supervisor-Recovery; `-RunInstallerExternalBackendSmoke` prueft External-Backend-Attach ohne Sidecar-Spawn; `-RunInstallerStartupTimeoutSmoke` prueft Supervisor-Ersatzstart nach nicht-ready Worker; `-RunInstallerStabilitySmoke` prueft wiederholte Health-/State-Probes und stabile Backend-PID; `-RunInstallerLegacyDataSmoke -RunInstallerUpgradeSmoke` prueft Legacy-Datenmigration und Datenerhalt ueber einen zweiten Installerlauf. Zusaetzlich optional `-EnableTauriUpdater` fuer signierte Tauri-Updater-Artefakte und Manifest-Signatur-Gates. Authenticode-Signing bleibt offen. |
| `scripts/smoke_windows_installer.ps1` | Umgesetzt: installiert das NSIS-Artefakt temporaer, prueft den installierten Tauri/Sidecar-Start ohne Python/Node-Dev-Fallback, kann mit `-SimulateBackendCrash` Worker-Recovery samt Crash-Metadata verifizieren, kann mit `-OccupyDefaultPort` die dynamische Portwahl bei belegtem Default-Port verifizieren, kann mit `-SimulateBackendShutdown` kontrolliertes Worker-Beenden plus Supervisor-Recovery verifizieren, kann mit `-AttachExternalBackend` das Andocken an ein externes Python-Backend ohne Sidecar-Spawn verifizieren, kann mit `-SimulateBackendStartupTimeout` einen nicht-ready Worker und Supervisor-Ersatzstart pruefen, kann mit `-StabilityDurationSec ...` wiederholte Health-/State-Probes und Backend-Working-Set-Samples erfassen, kann mit `-LegacyDataDir ... -VerifyLegacyDataMigration -SimulateUpgrade` Legacy-Datenmigration und Datenerhalt pruefen und entfernt die Testinstallation. |
| `scripts/sync_version.py` | Umgesetzt: synchronisiert `src/version.py` in Python/Tauri/Cargo/npm-Manifeste. |
| `scripts/create_release_metadata.py` | Umgesetzt: erzeugt `latest.json` und `SHA256SUMS.txt` fuer Release-Artefakte. |
| `.github/workflows/release-windows.yml` | Umgesetzt: manueller und Tag-basierter Windows-NSIS-Release-Build mit GitHub-Release-Publish auf `v*` Tags. |
| `LICENSE` | Umgesetzt: MIT License Datei. |

### Anzupassende Dateien
| Datei | Aenderung |
|-------|-----------|
| `src/tray.py` | Update-Menue-Eintraege, Launcher-Integration, `start_frontend()` konditional, Version im Tooltip. |
| `src/web_api.py` | Teilweise umgesetzt: Version/Runtime im Health-Endpoint, Session-Token-Middleware, `/api/runtime/shutdown` und `/api/runtime/support-bundle`. Offen: Update-Endpunkte (`/api/update/*`) und Static-File-Serving fuer Frontend im Frozen-Modus. |
| `src/config.py` | Update-Settings (`AUTO_UPDATE`, `UPDATE_CHANNEL`, `UPDATE_CHECK_INTERVAL_HOURS`, `UPDATE_URL`). |
| `Frontend/client/src/pages/Settings.tsx` | Update-Sektion im Settings-UI. |
| `README.md` | Install/Update User Guide fuer Endnutzer. |
| `src/youtube_download.py` | FFmpeg/FFprobe/YT-DLP-Pfad ueber zentrale Resolution (`runtime/media_tools.py`). |
| `src/audio_file_input.py` | FFmpeg-Pfad ueber zentrale Resolution. |
| `src/pipeline.py` | FFmpeg-Pfad ueber zentrale Resolution. |

---

## Akzeptanzkriterien
1. Frischer Windows-Rechner ohne Python/Node kann Scriber via Setup installieren und starten.
2. Neue GitHub Release wird innerhalb von **maximal 4 Stunden** automatisch erkannt (oder sofort bei manuellem Check).
3. Silent Update installiert erfolgreich und App startet danach wieder.
4. Nutzerdaten (`transcripts.db`, `settings.json`, `.env`, `downloads/`) bleiben bei Upgrade erhalten.
5. Update mit manipuliertem Installer (geaenderte SHA256 oder fehlende/falsche Signatur) wird sicher abgelehnt.
6. Downgrade-Versuch wird blockiert (mit klarer Fehlermeldung).
7. App startet nach dem Update mit dem korrekten neuen Versionslabel.
8. Update waehrend aktiver Aufnahme wird nicht ausgefuehrt, sondern auf Leerlauf verschoben.
9. Lite-Installer bleibt unter dem definierten Groessenbudget (Startwert: `<= 220 MB`) und wird pro Release automatisch geprueft.

---

## Offene Entscheidungen / Risiken

| # | Thema | Entscheidung noetig | Impact |
|---|-------|-------------------|--------|
| 1 | **EV vs. OV Zertifikat** | EV kann den Erstvertrauen-Eindruck verbessern, ist aber teurer und organisatorisch aufwendiger. OV ist guenstiger; SmartScreen-Reputation bleibt in beiden Faellen reputationsbasiert. | Hoch: bestimmt First-Install-Experience. |
| 2 | **Cloud-Signing vs. Hardware-Token/HSM** | Public-Trust Code-Signing verlangt hardwaregeschuetzte Schluessel. Empfehlung: Cloud-Signing (z. B. Microsoft Trusted Signing) fuer CI/CD statt lokalem Token-Handling. | Mittel: beeinflusst CI/CD-Setup. |
| 3 | **Lite vs. Full Distribution** | Soll der Standardnutzer nur Lite erhalten und lokale ASR als optionales Offline-Pack? Empfehlung: Ja. | Hoch: groesster Hebel auf Installer-Groesse. |
| 4 | **FFmpeg-Bundling** | Vollstaendige FFmpeg-Binary vs. schlanke Binary vs. On-Demand-Download. | Mittel: beeinflusst Setup-Groesse und Robustheit. |
| 5 | **Delta-Updates vs. Full-Installer** | Phase 1: Full-Installer (einfacher). Spaeter Delta nur fuer grosse veraenderliche Komponenten evaluieren. | Mittel: Bandbreiten-Optimierung. |
| 6 | **Frontend-Serving im Frozen-Modus** | Aktuell: Vite Dev Server. Ziel: aiohttp Static-File-Handler. Pruefe ob Express-Server (`Frontend/server/`) noch benoetigt wird oder ob aiohttp das komplett uebernehmen kann. | Mittel: Architektur-Entscheidung. |

---

## Optionale Zielstufe danach
Wenn Windows-first Distribution spaeter staerker Enterprise-orientiert wird:
1. Zweiter Kanal `MSIX + AppInstaller` fuer Managed Deployments.
2. Inno Kanal fuer breite Consumer-Kompatibilitaet behalten.
3. Chocolatey / winget als Zusatzkanaele fuer Power-User.
4. Auto-Update fuer portable ZIP-Version (separate Logik, niedrigere Prioritaet).

Diese Zielstufe ist sinnvoll, aber nicht Voraussetzung fuer einen robusten Auto-Update-Start.

---

## Validierte Quellen (Online-Check, Stand 2026-03-03)
1. Inno Setup Command-Line Parameter (`/VERYSILENT`, `/SUPPRESSMSGBOXES`, `/NORESTART`, `/SP-`, `/CLOSEAPPLICATIONS`): https://jrsoftware.org/ishelp/topic_setupcmdline.htm
2. Inno Setup Setup-Optionen (`CloseApplications`, `AppMutex`): https://jrsoftware.org/ishelp/topic_setupsection.htm
3. GitHub REST API Releases (`/releases/latest`, release assets, non-draft/non-prerelease): https://docs.github.com/en/rest/releases/releases#get-the-latest-release
4. GitHub Release semantics (`latest` label / `make_latest`): https://github.blog/changelog/2022-10-21-explicitly-set-the-latest-release/
5. Squirrel.Windows (auch fuer Nicht-.NET/Native Apps nutzbar): https://github.com/Squirrel/Squirrel.Windows
6. Microsoft App Installer Update Settings (Version-Hinweise ab Windows 10, 1709): https://learn.microsoft.com/windows/msix/app-installer/update-settings
7. Microsoft Trusted Signing (Cloud-Signing fuer Code-Signing-Workflows): https://learn.microsoft.com/azure/trusted-signing/overview
8. PowerShell `Get-AuthenticodeSignature`: https://learn.microsoft.com/powershell/module/microsoft.powershell.security/get-authenticodesignature
9. `SignatureStatus` Enum (`Valid`, `NotSigned`, etc.): https://learn.microsoft.com/dotnet/api/system.management.automation.signaturestatus
10. MSIX Integritaetsmodell (Package + Block Map Hashes): https://learn.microsoft.com/windows/msix/package/app-package-format
