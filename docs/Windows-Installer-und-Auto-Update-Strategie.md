# Windows Installer und Auto-Update Strategie fuer Scriber

## Ziel
Nicht versierte Nutzer sollen Scriber wie eine normale Windows-App installieren und automatisch auf dem aktuellen Stand bleiben, ohne Python, Node oder manuelle Update-Schritte.

## Empfohlene Zielvariante (optimal fuer Scriber jetzt)
### Primarer Kanal
`Inno Setup` + `eigener Updater` + `GitHub Releases` + `Authenticode Signierung`

### Warum diese Variante
1. Passt zur aktuellen Python-Architektur ohne Re-Write.
2. Schnell produktionsfaehig im Vergleich zu MSIX-only oder Squirrel-Integration.
3. Silent Updates sind robust moeglich (`/VERYSILENT /SUPPRESSMSGBOXES /NORESTART /SP-`).
4. Security kann sauber gehaertet werden (SHA256, Signaturpruefung, immutable Releases).

### Warum kein Electron/Tauri/MSIX jetzt
| Alternative        | Warum nicht jetzt                                                                       |
|--------------------|----------------------------------------------------------------------------------------|
| Electron / Tauri   | Erfordert vollstaendigen Re-Write des Backends. Scriber ist eine Python-App.            |
| MSIX + AppInstaller| Erfordert saubere MSIX-Paketierung + Signierung; Update-Settings sind je nach Windows-Version unterschiedlich verfuegbar (Basis seit Windows 10, Version 1709). |
| Squirrel.Windows   | Auch fuer Nicht-.NET-Apps nutzbar, aber NuGet/Squirrel-Artefaktmodell passt nicht direkt zur aktuellen Python-Build-Pipeline und erhoeht den Integrationsaufwand. |
| PyUpdater / Esky   | Nicht mehr aktiv gepflegt (Stand 2026). Eigener Updater ist transparenter und robuster. |

### Zielbild in einem Satz
Scriber laeuft als signierte, installierte Windows-App (per-user), prueft im Hintergrund Releases, laedt signierte Updates, installiert sie still im Leerlauf und startet sich neu.

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
   - Aktuell erwartet der Code `shutil.which("ffmpeg")` in `youtube_download.py`, `web_api.py`, `audio_file_input.py` und `pipeline.py`.
   - Fuer die gebuendelte Version: FFmpeg-Binary in `dist/app/ffmpeg/` mitliefern und `PATH` zur Laufzeit erweitern.
   - Alternativ: Pfad-Resolution in einer zentralen Funktion (`src/runtime/ffmpeg.py`) kapseln, die sowohl Dev-Modus (System-PATH) als auch Frozen-Modus (gebuendelter Pfad) unterstuetzt.

### 2) Installer
1. **Inno Setup**, per-user Install nach `%LocalAppData%\Scriber`.
   - Per-user vermeidet UAC-Prompts und erfordert keine Admin-Rechte.
   - Stabiler `AppId` im Format `{GUID}` fuer saubere Upgrade-Erkennung.
2. **Startmenue-Eintrag**, optional **Autostart** via Registry `HKCU\Software\Microsoft\Windows\CurrentVersion\Run`.
   - Autostart existiert bereits als Feature in `web_api.py` (`GET/POST /api/autostart`). Integration mit Installer pruefen.
3. **Uninstall** ohne Loeschen von Nutzerdaten.
   - Folgende Dateien/Ordner muessen in `[UninstallDelete]` **ausgeschlossen** werden:
     - `transcripts.db` (+ WAL/SHM-Dateien)
     - `settings.json`
     - `.env`
     - `downloads/` (heruntergeladene YouTube-Audio-Dateien)
   - Inno Setup: `[InstallDelete]` fuer alte Versionsdateien nutzen, aber Nutzerdaten unangetastet lassen.
4. **CloseApplications-Unterstuetzung**: Inno Setup `CloseApplications=yes` damit laufende Scriber-Instanzen vor dem Upgrade sauber heruntergefahren werden.
   - Voraussetzung: Die Scriber-App muss eine `AppMutex` registrieren, die Inno Setup nutzen kann.
   - Aktuell existiert bereits `acquire_single_instance_lock()` in `tray.py` (Zeile 74-100). Diese kann als Grundlage fuer eine Named Mutex dienen.

### 3) Updater-Komponente
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

Beispiel `latest.json`:
```json
{
  "version": "1.4.0",
  "channel": "stable",
  "published_at": "2026-03-03T18:00:00Z",
  "installer_url": "https://github.com/MyButtermilk/Scriber/releases/download/v1.4.0/Scriber-Setup-x64.exe",
  "installer_size_bytes": 85000000,
  "sha256": "a1b2c3d4e5f6...",
  "notes_url": "https://github.com/MyButtermilk/Scriber/releases/tag/v1.4.0",
  "release_notes": "Neue Features: ...",
  "min_supported_version": "1.2.0",
  "min_updater_version": "1.0.0"
}
```

Felder gegenueber Entwurf ergaenzt:
- `installer_size_bytes`: Ermoeglicht Speicherplatzpruefung und Fortschrittsanzeige vor dem Download.
- `release_notes`: Inline Release-Notes fuer direkten Anzeige im Update-Dialog, ohne URL-Abruf.
- `min_updater_version`: Falls der Launcher selbst ein kritisches Update benoetigt.

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
1. `requirements.txt` aufteilen in:
   - `requirements-base.txt` (Runtime fuer Standardnutzer),
   - `requirements-local-asr.txt` (nur lokale ASR-Provider),
   - `requirements-dev.txt` (Tests/Tools).
2. Release-Build nutzt nur `requirements-base.txt`.
3. Lokale Provider werden per **Lazy Import** geladen (erst wenn Nutzer sie aktiviert).
4. Nicht genutzte Provider im Standard-Build per Feature-Flag deaktivieren.

### C) PyInstaller gezielt schlank halten
1. In `scriber.spec` nur benoetigte `hiddenimports`/`datas` aufnehmen.
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
   - README referenziert `LICENSE` bereits (Zeile 470), die Datei existiert aber noch nicht im Root.
2. **Verbindliche Versionierung** einfuehren:
   - Neue Datei `src/version.py` mit `__version__ = "1.0.0"`.
   - SemVer-Format: `MAJOR.MINOR.PATCH`.
   - Importiert in `config.py`, `tray.py` (Tooltip), `web_api.py` (Health-Endpoint, neues Feld `version`).
   - Wird von `build_windows.ps1` als Quelle fuer die Installer-Versionsnummer verwendet.
3. Zielplattform fixieren: **Windows 10/11 x64** (Build 1809+).
4. Install Scope festlegen: **per-user** (empfohlen, kein UAC).
5. **Frozen-Mode-Erkennung** einfuehren:
   - Zentrale Hilfsfunktion `is_frozen()` basierend auf `getattr(sys, 'frozen', False)`.
   - Pfad-Resolution fuer Data-Dateien (`assets/`, `Frontend/dist/`) anpassen: `sys._MEIPASS` im Frozen-Modus vs. `Path(__file__).parent` im Dev-Modus.
   - Betrifft: `tray.py` (Icon-Lade-Pfad), `web_api.py` (Static-File-Serving), `config.py` (Settings-Pfade).

### Phase 1 - Produktisierbarer Build
1. **Frontend Production Build** in den Backend-Output integrieren:
   - `npm run build` erzeugt `Frontend/dist/public/` mit statischem HTML/JS/CSS.
   - `build_windows.ps1` kopiert diesen Output in das PyInstaller-Output-Verzeichnis.
   - `web_api.py` erhaelt einen Static-File-Handler, der im Frozen-Modus `dist/public/` als Root served.
2. **Tray/Backend Start umbauen**: `start_frontend()` in `tray.py` darf im Frozen-Modus **nicht** `npm run dev:client` starten.
   - Konditional: Wenn `is_frozen()`, dann kein Frontend-Prozess starten; die statischen Dateien werden direkt vom Backend geserved.
   - Falls nicht frozen (Dev-Modus): bestehendes Verhalten beibehalten.
3. **Freeze Build** erstellen:
   - PyInstaller `onedir`, Zielverzeichnis `dist/app/`.
   - Entry-Point: `src/tray.py` (startet Backend als Subprocess, managed Lifecycle).
   - Hidden imports explizit listen (basierend auf Codebase-Analyse):
     - `pipecat-ai` Submodule (google, assemblyai, silero, deepgram, openai, azure, gladia, groq, speechmatics, aws, elevenlabs)
     - `sounddevice`, `pycaw`, `keyboard`, `pyautogui`
     - `PySide6`, `pystray`, `customtkinter`
     - `onnx-asr`, `nemo_toolkit`
     - `yt-dlp`, `python-docx`, `reportlab`, `lxml`
   - Daten-Dateien einschliessen: `src/assets/`, `Frontend/dist/public/`, FFmpeg-Binary.
4. **Reproduzierbarer Build** per Script.
5. **Size-Profiling direkt in Phase 1**:
   - `requirements-base/local-asr/dev` einfuehren.
   - Lite-Build als Standard in `build_windows.ps1`.
   - Groessenreport (`size-report.json`) erzeugen und in CI publizieren.

Lieferobjekte:
1. `scripts/build_windows.ps1`
2. `scriber.spec` (PyInstaller-Konfiguration)
3. `src/version.py`
4. `src/runtime/paths.py` (Frozen/Dev Pfad-Resolution)
5. `requirements-base.txt`, `requirements-local-asr.txt`, `requirements-dev.txt`
6. `size-report.json` (CI-Artefakt)
7. Start/Healthcheck fuer gebaute App (manueller Smoke-Test)

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
     AppMutex=ScriberSingleInstanceMutex
     UninstallDisplayIcon={app}\Scriber.exe
     ```
   - `[Files]`: Alle Dateien aus `dist/app/`.
   - `[Icons]`: Startmenue-Link + optionaler Desktop-Link.
   - `[Run]`: App nach Installation starten (optional, `postinstall` Flag).
   - `[UninstallDelete]`: Gezielt nur App-Dateien, niemals `transcripts.db`, `settings.json`, `.env`, `downloads/`.
2. **Install/Upgrade/Uninstall** inkl. Datenerhalt testen:
   - Testmatrix: Frischinstallation, Upgrade mit laufender App, Upgrade mit Nutzerdaten, Deinstallation.
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
   - Trigger: Push von Tags `v*`.
   - Matrix: Windows-latest, Python 3.11+, Node 20+.
2. **Build-Pipeline**:
   - Checkout → Python/Node Setup → `pip install` → `npm ci && npm run build` (Frontend) → PyInstaller → Inno Setup → Signierung → Checksums → Release Upload.
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
| `src/version.py` | Zentrale Versionsnummer (`__version__`). |
| `src/updater.py` | UpdateManager: Check, Download, Verify, Install. |
| `src/runtime/paths.py` | Frozen/Dev Pfad-Resolution (Data-Dateien, FFmpeg, Frontend). |
| `installer/scriber.iss` | Inno Setup Script. |
| `scripts/build_windows.ps1` | Build-Pipeline: Frontend Build → PyInstaller → Inno Setup. |
| `.github/workflows/release-windows.yml` | CI/CD fuer Tag-basierte Releases. |
| `LICENSE` | MIT License Datei. |

### Anzupassende Dateien
| Datei | Aenderung |
|-------|-----------|
| `src/tray.py` | Update-Menue-Eintraege, Launcher-Integration, `start_frontend()` konditional, Version im Tooltip. Named Mutex fuer Inno Setup. |
| `src/web_api.py` | Update-Endpunkte (`/api/update/*`), Static-File-Serving fuer Frontend im Frozen-Modus, Version im Health-Endpoint. |
| `src/config.py` | Update-Settings (`AUTO_UPDATE`, `UPDATE_CHANNEL`, `UPDATE_CHECK_INTERVAL_HOURS`, `UPDATE_URL`). |
| `Frontend/client/src/pages/Settings.tsx` | Update-Sektion im Settings-UI. |
| `README.md` | Install/Update User Guide fuer Endnutzer. |
| `src/youtube_download.py` | FFmpeg-Pfad ueber zentrale Resolution (`runtime/paths.py`). |
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
