# Product Requirements Document – Scriber

Du bist ein erfahrener Software-Architekt. Erstelle die Anwendung "Scriber" exakt nach folgender Spezifikation. Dies ist ein vollständiges PRD des aktuellen Implementierungsstandes.

---

## 1. Produktübersicht

**Scriber** ist eine Desktop- und Web-Anwendung für KI-gestützte Sprach-Transkription. Sie bietet drei Transkriptions-Modi (Live-Mikrofon, YouTube, Datei-Upload), unterstützt 14+ STT-Provider (Cloud + lokal), speichert Transkripte persistent und kann diese zusammenfassen und exportieren.

**Architektur:** Python-Backend (aiohttp, Port 8765) + React-Frontend (Vite/Express, Port 5000) + System-Tray-Integration. Kommunikation über REST-API und WebSocket.

**Plattform:** Primär Windows, grundlegende Linux/macOS-Unterstützung.

---

## 2. Technologie-Stack

### Backend
- **Sprache:** Python 3.10+
- **Web-Framework:** aiohttp (async HTTP + WebSocket)
- **Audio-Pipeline:** pipecat-ai (mit Provider-Extras für Soniox, Deepgram, OpenAI, Azure, Gladia, Groq, Speechmatics, ElevenLabs, Google, AWS, AssemblyAI, Silero-VAD)
- **Datenbank:** SQLite mit WAL-Modus, FTS5-Volltextsuche, thread-lokale Connections
- **Audio-Capture:** sounddevice, pycaw (Windows Volume Control)
- **Hotkey:** keyboard-Bibliothek (globaler Hotkey)
- **YouTube:** yt-dlp (Download), YouTube Data API v3 (Suche/Metadaten)
- **Summarization:** google-generativeai (Gemini 3.x), openai (GPT-5.x)
- **Export:** reportlab (PDF), python-docx (DOCX)
- **Tray:** pystray + Pillow (System-Tray-Icon)
- **Overlay:** PySide6-Essentials / customtkinter (Recording-Overlay)
- **Logging:** loguru (strukturiert + human-readable)
- **Konfiguration:** python-dotenv (.env) + settings.json

### Frontend
- **Framework:** React 19.2 mit TypeScript 5.6
- **Routing:** wouter 3.3 (leichtgewichtig, nicht React Router)
- **Server State:** @tanstack/react-query 5.60
- **UI-Komponenten:** Radix UI (20+ Primitives), lucide-react (Icons)
- **Styling:** Tailwind CSS 4.x, class-variance-authority, tailwind-merge
- **Formulare:** react-hook-form 7.66 + zod 3.25 (Validierung)
- **Animation:** framer-motion 12.x
- **Notifications:** sonner 2.0
- **File Upload:** react-dropzone 14.3
- **Markdown:** react-markdown 10.1
- **Build:** Vite 7.1
- **Server:** Express 4.21 (Dev + Prod)

---

## 3. System-Architektur

```
Browser/Hotkey/Tray ──HTTP+WS──► Python Backend (src.web_api, :8765)
                                    ├── ScriberWebController
                                    │   ├── Pipeline + ProviderRouter
                                    │   ├── SQLite (transcripts.db)
                                    │   ├── JobStore + RetryScheduler
                                    │   └── Provider / Local Models
                                    └── WebSocket /ws (Echtzeit-Events)

React UI (Frontend/client, :5000) ◄──REST+WS──► Backend
```

### Laufzeitpfade
- **Live Mic:** `POST /api/live-mic/{start|stop|toggle}` → Pipeline → Live-Events → Transcript-Persistierung
- **YouTube:** `POST /api/youtube/transcribe` → yt-dlp Download → Pipeline → Retry/Resume
- **Datei:** `POST /api/file/transcribe` (multipart) → optional ffmpeg-Extraktion → Pipeline
- **Kommunikation:** REST für Steuerung, WebSocket `/ws` für Status-/Live-Daten

---

## 4. Datenmodell

### SQLite-Schema (`transcripts.db`)

```sql
CREATE TABLE transcripts (
    id TEXT PRIMARY KEY,
    title TEXT,
    date TEXT,
    duration REAL,
    status TEXT,          -- queued, transcribing, completed, failed, cancelled
    type TEXT,            -- mic, youtube, file
    language TEXT,
    step TEXT,
    source_url TEXT,
    channel TEXT,
    thumbnail_url TEXT,
    content TEXT,
    preview TEXT,         -- Vorschau für Listen (kein vollständiger Content)
    summary TEXT,
    created_at TEXT,
    updated_at TEXT
);

-- FTS5-Volltextsuche
CREATE VIRTUAL TABLE transcripts_fts USING fts5(title, content, preview);

-- Index für schnelle Sortierung
CREATE INDEX idx_created_at ON transcripts(created_at DESC);
```

**DB-Features:**
- WAL-Modus für parallele Lesezugriffe
- Thread-lokale Connections (eine pro Thread)
- Paginierung: `offset`/`limit` (max 100 pro Seite, default 50)
- Preview-Spalte für schnelle Listenansicht ohne Content-Load

### Dateispeicher
- `downloads/` – YouTube-Audio-Downloads
- `transcripts.db` + `.db-shm` + `.db-wal` – Datenbank
- `settings.json` – Persistierte Einstellungen (z.B. Summarization-Prompt)
- `latest.log` / `latest.structured.jsonl` – Anwendungslogs

---

## 5. Features im Detail

### 5.1 Live-Mikrofontranskription

**Trigger:** Globaler Hotkey (Default: `Ctrl+Alt+S`, konfigurierbar) oder UI-Button.

**Modi:**
- `toggle` (start_stop): Hotkey startet/stoppt Aufnahme abwechselnd
- `push_to_talk` (press_hold): Aufnahme nur solange Hotkey gehalten wird

**Pipeline:**
1. Audio-Capture über sounddevice (konfigurierbare Block-Size, Default 512)
2. VAD (Voice Activity Detection) via Silero
3. SmartTurn-Analyse für Satzgrenzen
4. STT-Provider verarbeitet Audio-Frames
5. Echtzeit-Ergebnisse über WebSocket an Frontend
6. Finale Transkription → SQLite-Persistierung
7. Optional: Text-Injection in aktive Anwendung

**Text-Injection-Methoden:**
- `auto` – Intelligent: SendInput für Standard-Apps, Paste für Word/Outlook
- `sendinput` – Windows SendInput API (Batch, schnell)
- `paste` – Clipboard + Ctrl+V (zuverlässig)
- `type` – Zeichenweise Tastatureingabe (langsamste, kompatibelste)

**WebSocket-Events:**
- `state` – Globaler Zustand
- `status` – Textnachrichten
- `transcript` – Live-Transkriptionstext
- `audio_level` – Audiopegelanzeige
- `input_warning` – Eingabewarnungen
- `session_started` / `session_finished` – Session-Lifecycle
- `history_updated` – Transcript-Liste aktualisiert
- `error` – Fehlermeldungen

**Mikrofon-Features:**
- Geräteliste über `GET /api/microphones`
- Favoriten-Mikrofon (automatische Auswahl bei Verfügbarkeit)
- Hotplug-Erkennung (USB-Mikrofone automatisch erkannt via DeviceMonitor)
- `MIC_ALWAYS_ON` – Mikrofon dauerhaft geöffnet halten

### 5.2 YouTube-Transkription

**Workflow:**
1. Suche über YouTube Data API v3 (`GET /api/youtube/search`)
2. Video-Info abrufen (`GET /api/youtube/video?id=...` oder `?url=...`)
3. Transkriptionsjob starten (`POST /api/youtube/transcribe`)
4. Download via yt-dlp mit Fortschritts-Tracking
5. Audio-Extraktion (WebM/Opus, 16kHz Mono)
6. STT-Pipeline-Verarbeitung
7. Persistierung als Transcript (type: "youtube")

**Job-Status-Lifecycle:** `queued → transcribing → completed | failed`

**Retry-Mechanik:**
- Max Versuche: konfigurierbar (Default: 3)
- Exponential Backoff: Base 5s, Max 120s
- Automatisches Resume nach Backend-Neustart

### 5.3 Datei-Transkription

**Endpoint:** `POST /api/file/transcribe` (multipart/form-data, Feld: `file`)

**Unterstützte Formate:**
- Audio: `.mp3`, `.wav`, `.m4a`, `.flac`, `.aac`, `.ogg`
- Video: `.mp4`, `.mov`, `.webm`, `.avi`, `.mkv`, `.m4v`

**Limits:**
- Audio: 200 MB (konfigurierbar via `SCRIBER_UPLOAD_MAX_MB`)
- Video-Rohdatei: 2048 MB (hartes Limit)
- Extrahiertes Audio aus Video: 200 MB

**Video-Verarbeitung:** ffmpeg-Extraktion → WebM/Opus, 16kHz, Mono

**Sicherheit:** Dateinamen-Sanitisierung (Windows-reservierte Namen, ungültige Zeichen), Extension-Whitelist

### 5.4 STT-Provider-System

**14+ Provider:**

| Provider | Modus | Modelle |
|----------|-------|---------|
| Soniox | Realtime + Async | stt-rt-v4, stt-async-v4 |
| Mistral | Realtime + Async | voxtral-mini-transcribe-realtime-2602, voxtral-mini-2602 |
| AssemblyAI | Async | Universal-3-Pro (Speaker Diarization) |
| Deepgram | Cloud | Standard |
| OpenAI | Cloud | gpt-4o-mini-transcribe |
| ElevenLabs | Cloud | Standard |
| Azure Speech | Cloud | Cognitive Services |
| Gladia | Cloud | Standard |
| Groq | Cloud | Fast Inference |
| Speechmatics | Cloud | Enterprise |
| Google Cloud | Cloud | Speech-to-Text |
| AWS Transcribe | Cloud | Amazon STT |
| ONNX | Lokal | NeMo Parakeet TDT 0.6B (int8/fp16/fp32) |
| NeMo | Lokal | Parakeet Primeline |

**Infrastruktur:**
- `ProviderRouter` – Zuordnung und Auswahl des aktiven Providers
- `CircuitBreaker` – Automatische Abschaltung bei wiederholten Fehlern
- `RetryScheduler` – Exponential Backoff bei temporären Fehlern
- Benutzerdefiniertes Vokabular (`SCRIBER_CUSTOM_VOCAB`)

### 5.5 Zusammenfassung (Summarization)

**Provider:**
- Google Gemini 3.0 Flash/Pro Preview (Default)
- OpenAI GPT-5.x

**Features:**
- Dynamisches Token-Budget basierend auf Input-Länge
- Markdown-Output mit Längeninstruktionen
- Thinking-Reserve für Gemini-Modelle
- Auto-Summarize bei Job-Abschluss (optional, `SCRIBER_AUTO_SUMMARIZE`)
- Konfigurierbarer Prompt (persistiert in `settings.json`)
- Mindest-/Maximalwortanzahl konfigurierbar

**API:** `POST /api/transcripts/{id}/summarize`

### 5.6 Transcript-Management

**CRUD-Operationen:**
- Liste mit Paginierung, Typ-Filter, Volltextsuche
- Einzelansicht mit vollständigem Content + Summary
- Löschen einzelner Transcripts
- Job-Abbruch für laufende Transkriptionen

**Export:**
- PDF (via ReportLab) – mit Markdown-Parsing (Überschriften, Listen, Fett/Kursiv), Metadaten-Header (Datum, Dauer)
- DOCX (via python-docx) – gleiches Markdown-Parsing, Metadaten

**API:** `GET /api/transcripts/{id}/export/{pdf|docx}`

### 5.7 Lokale Modelle

**ONNX-Management:**
- Modellliste, Download mit Fortschritt, Löschung
- Quantisierung: int8, fp16, fp32
- GPU-Unterstützung optional

**NeMo-Management:**
- Modellliste, Download, Löschung
- NeMo Toolkit Integration

**API-Endpunkte:**
- `GET/POST/DELETE /api/onnx/models[/{model_id}]`
- `GET/POST/DELETE /api/nemo/models[/{model_id}]`

---

## 6. Frontend-Seiten

### 6.1 Live Mic (`/`)
- Glossy Mikrofon-Button mit Audio-Visualizer (konfigurierbare Bar-Anzahl)
- Echtzeit-Transkript-Anzeige
- History-Liste mit Suche und Typ-Filter
- Grid-/List-View-Toggle
- Löschung mit Glitch-Animation
- Backend-Offline-Banner bei Verbindungsverlust

### 6.2 YouTube (`/youtube`)
- Suchfeld mit YouTube Data API-Integration
- Video-Preview-Cards (Thumbnail, Titel, Channel, Dauer)
- Transkriptions-Start per Klick
- Job-Queue-Übersicht mit Fortschritt
- History-Liste

### 6.3 File Upload (`/file`)
- Drag-and-Drop-Zone (react-dropzone)
- Format-Erkennung (Audio vs. Video)
- Upload-Fortschritt
- Ergebnisliste

### 6.4 Transcript Detail (`/transcript/:id`)
- Vollständige Content-Anzeige
- Summary-Sektion (Markdown-Rendering via react-markdown)
- Export-Aktionen (PDF/DOCX)
- Zusammenfassen-Button
- Löschen/Abbrechen-Aktionen
- Auto-Refresh für aktive Jobs

### 6.5 Settings (`/settings`)
- **Hotkey:** Konfiguration des globalen Hotkeys
- **Modus:** Toggle vs. Push-to-Talk
- **STT-Provider:** Dropdown-Auswahl aller Provider
- **Provider-spezifisch:** Soniox-Modus/Modell, Mistral-Modell, OpenAI-Modell
- **Sprache:** Sprachauswahl
- **API-Keys:** Gruppierte Eingabe aller Provider-Keys
- **Mikrofon:** Geräteauswahl + Favorit + Always-On
- **Injection:** Methoden-Auswahl (auto/sendinput/paste/type) + Delays
- **Lokale Modelle:** ONNX-/NeMo-Download/Lösch-Management mit Fortschritt
- **Zusammenfassung:** Modell, Auto-Summarize Toggle, Custom Prompt (Textarea)
- **Visualizer:** Bar-Anzahl

---

## 7. API-Spezifikation

### System

| Methode | Pfad | Beschreibung |
|---------|------|-------------|
| GET | `/api/health` | Health Check |
| GET | `/api/state` | Globaler App-State |
| GET | `/api/metrics/hot-path?limit=n` | Performance-Metriken (limit: 1–500) |

### WebSocket

| Pfad | Events |
|------|--------|
| `/ws` | state, status, transcript, audio_level, input_warning, transcribing, session_started, session_finished, history_updated, error |

### Live Mic

| Methode | Pfad |
|---------|------|
| POST | `/api/live-mic/start` |
| POST | `/api/live-mic/stop` |
| POST | `/api/live-mic/toggle` |

### Transcripts

| Methode | Pfad | Parameter |
|---------|------|-----------|
| GET | `/api/transcripts` | `?offset=0&limit=50&type={mic\|youtube\|file}&q={query}` |
| GET | `/api/transcripts/{id}` | |
| DELETE | `/api/transcripts/{id}` | |
| POST | `/api/transcripts/{id}/summarize` | |
| POST | `/api/transcripts/{id}/cancel` | |
| GET | `/api/transcripts/{id}/export/{format}` | format: pdf, docx |

### YouTube

| Methode | Pfad | Parameter |
|---------|------|-----------|
| GET | `/api/youtube/search` | `?q={query}&maxResults={n}&pageToken={token}` |
| GET | `/api/youtube/video` | `?id={id}` oder `?url={url}` |
| POST | `/api/youtube/transcribe` | Body: video_id, title, etc. |

### Datei

| Methode | Pfad | Body |
|---------|------|------|
| POST | `/api/file/transcribe` | multipart/form-data, Feld: file |

### Settings

| Methode | Pfad |
|---------|------|
| GET | `/api/settings` |
| PUT | `/api/settings` |
| GET | `/api/microphones` |
| GET | `/api/autostart` |
| POST | `/api/autostart` |

### Lokale Modelle

| Methode | Pfad |
|---------|------|
| GET | `/api/onnx/models` |
| GET | `/api/onnx/models/{model_id}` |
| POST | `/api/onnx/download` |
| DELETE | `/api/onnx/models/{model_id}` |
| GET | `/api/nemo/models` |
| POST | `/api/nemo/download` |
| DELETE | `/api/nemo/models/{model_id}` |

---

## 8. Konfiguration

### Environment Variables (.env)

**Basis:**
- `SCRIBER_WEB_HOST` (default: 127.0.0.1)
- `SCRIBER_WEB_PORT` (default: 8765)
- `SCRIBER_ALLOWED_ORIGINS` (default: localhost,127.0.0.1,::1; unterstützt Wildcard `*`)

**Audio/Verhalten:**
- `SCRIBER_HOTKEY` (default: ctrl+alt+s)
- `SCRIBER_MODE` (toggle | push_to_talk)
- `SCRIBER_DEFAULT_STT` (provider name)
- `SCRIBER_SONIOX_MODE` (realtime | async)
- `SCRIBER_LANGUAGE` (ISO-Code oder "auto")
- `SCRIBER_DEBUG` (0 | 1)
- `SCRIBER_CUSTOM_VOCAB` (Komma-getrennte Wörter)

**Mikrofon:**
- `SCRIBER_MIC_DEVICE` (default | Gerätename)
- `SCRIBER_FAVORITE_MIC` (Gerätename für Auto-Auswahl)
- `SCRIBER_MIC_ALWAYS_ON` (0 | 1)
- `SCRIBER_MIC_BLOCK_SIZE` (default: 512)

**Injection:**
- `SCRIBER_INJECT_METHOD` (auto | sendinput | paste | type)
- `SCRIBER_PASTE_PRE_DELAY_MS` (default: 80)
- `SCRIBER_PASTE_RESTORE_DELAY_MS` (default: 1500)

**Uploads/Jobs:**
- `SCRIBER_UPLOAD_MAX_MB` (default: 200)
- `SCRIBER_DOWNLOADS_DIR` (default: ./downloads)
- `SCRIBER_JOB_MAX_ATTEMPTS` (default: 3)
- `SCRIBER_JOB_RETRY_BASE_SEC` (default: 5)
- `SCRIBER_JOB_RETRY_MAX_SEC` (default: 120)
- `SCRIBER_TIMEOUT_FILE_TRANSCRIBE_SEC` (default: 600)
- `SCRIBER_TIMEOUT_YOUTUBE_TRANSCRIBE_SEC` (default: 600)
- `SCRIBER_TIMEOUT_YOUTUBE_DOWNLOAD_SEC` (default: 300)

**Summarization:**
- `SCRIBER_SUMMARIZATION_MODEL` (default: gemini-3-flash-preview)
- `SCRIBER_AUTO_SUMMARIZE` (0 | 1)
- `SCRIBER_SUMMARY_MIN_WORDS` (default: 180)
- `SCRIBER_SUMMARY_MAX_WORDS` (default: 2200)

**API-Keys:**
- `SONIOX_API_KEY`, `MISTRAL_API_KEY`, `ASSEMBLYAI_API_KEY`, `DEEPGRAM_API_KEY`, `OPENAI_API_KEY`, `AZURE_SPEECH_KEY`, `AZURE_SPEECH_REGION`, `GLADIA_API_KEY`, `GROQ_API_KEY`, `SPEECHMATICS_API_KEY`, `ELEVENLABS_API_KEY`, `GOOGLE_API_KEY`, `YOUTUBE_API_KEY`
- AWS via Standard-Env: `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`, `AWS_REGION`

**Lokale Modelle:**
- `SCRIBER_ONNX_MODEL`, `SCRIBER_ONNX_QUANTIZATION` (int8|fp16|fp32), `SCRIBER_ONNX_USE_GPU` (0|1)
- `SCRIBER_NEMO_MODEL`

**UI:**
- `SCRIBER_VISUALIZER_BAR_COUNT` (default: 60)

### Persistierte Settings (settings.json)
- `summarizationPrompt` – Mehrzeiliger LLM-Prompt für Zusammenfassungen

---

## 9. Startup & Deployment

### Windows (start.bat)
1. Python-Prüfung → venv erstellen/aktivieren
2. requirements.txt installieren (Hash-basiertes Caching)
3. .env erstellen falls nicht vorhanden
4. Start: `python -m src.tray` (wenn Node + Frontend vorhanden), sonst `python -m src.main`
5. Browser öffnen: `http://localhost:5000`

### Tray-Modus (src.tray)
- Single-Instance-Lock via `.scriber.lock`
- Startet Backend (`python -m src.web_api`) als Subprocess
- Startet Frontend (Express) als Subprocess
- System-Tray-Icon mit Kontextmenü: Öffnen, Logs, Restart, Autostart (Windows Registry), Beenden
- Process-Watchdog für automatischen Neustart

### Direkt-Betrieb
- Backend: `python -m src.web_api` (Port 8765)
- Frontend Dev: `cd Frontend && npm run dev:client` (Port 5000)
- Frontend Prod: `cd Frontend && npm run build && npm start`

---

## 10. Performance-Optimierungen

### Backend
- Thread-lokale DB-Connections (kein wiederholtes Öffnen/Schließen)
- WAL-Modus für parallele Lesezugriffe
- Analyzer-Caching (VAD, SmartTurn) für schnelleren Pipeline-Start
- SpooledTemporaryFile für große Audio-Buffer (10 MB RAM-Cap)
- WebSocket Event-Batching (history_updated)
- Circuit Breaker gegen wiederholte Provider-Fehler
- Hot-Path-Tracing für Latenz-Monitoring

### Frontend
- Lazy Loading für schwere Routen (YouTube, Settings)
- React Query Caching mit stale-while-revalidate
- Memoized Components (TranscriptCard)
- Reduced-Motion-Detection
- Optimistic UI Updates
- Single shared WebSocket Connection

### Datenbank
- Preview-Spalte für schnelle Listenansicht
- FTS5 für Volltextsuche
- Index auf created_at für schnelle Sortierung
- Paginierung zur Ergebnis-Limitierung

---

## 11. Core-Infrastruktur (src/core/)

- **Error Taxonomy** – Strukturierte Fehlerklassifikation
- **Circuit Breaker** – Automatische Provider-Abschaltung bei Fehlermustern
- **State Machine** – Zustandsmanagement für Jobs und Pipeline
- **Logging** – Strukturierte + menschenlesbare Logs (loguru)

---

## 12. Teststruktur

**Framework:** pytest + pytest-asyncio + pytest-mock

**Coverage (21 Testdateien):**
- Pipeline-Lifecycle, Stop-Verhalten
- Text-Injection (alle Methoden)
- YouTube API + Download
- Web API: Security, Jobs, Lifecycle, Timeouts, Reliability, Hot-Path-Metriken
- Mikrofon: Geräteauswahl, Kanalhandling
- AssemblyAI-Integration
- Summarization (LLM-Aufrufe)
- Konfiguration

---

## 13. Projektstruktur

```
Scriber/
├── src/
│   ├── web_api.py              # aiohttp REST + WebSocket API (Hauptmodul)
│   ├── pipeline.py             # STT-Pipeline / Provider-Orchestrierung
│   ├── config.py               # .env + settings.json Konfiguration
│   ├── database.py             # SQLite (transcripts.db, FTS5, WAL)
│   ├── tray.py                 # System Tray, Watchdog, Autostart
│   ├── main.py                 # Tkinter-Fallback UI
│   ├── export.py               # PDF/DOCX Export
│   ├── overlay.py              # Recording-Overlay (PySide6/Tkinter)
│   ├── summarization.py        # LLM-Zusammenfassung (Gemini/OpenAI)
│   ├── youtube_api.py          # YouTube Data API v3
│   ├── youtube_download.py     # yt-dlp Audio-Download
│   ├── assemblyai_async_stt.py # AssemblyAI Universal-3-Pro
│   ├── mistral_stt.py          # Mistral Voxtral
│   ├── audio_devices.py        # Mikrofon-Enumeration
│   ├── device_monitor.py       # USB-Hotplug-Erkennung
│   ├── microphone.py           # Audio-Capture
│   ├── injector.py             # Text-Injection (SendInput/Paste/Type)
│   ├── onnx_local_service.py   # ONNX STT
│   ├── nemo_local_service.py   # NeMo STT
│   ├── core/                   # Shared Utilities
│   │   ├── error_taxonomy.py
│   │   ├── circuit_breaker.py
│   │   ├── state_machine.py
│   │   └── logging.py
│   ├── data/                   # Datenmodelle
│   │   ├── job_store.py
│   │   └── latency_metrics.py
│   └── runtime/                # Laufzeit-Services
│       ├── provider_router.py
│       └── retry_scheduler.py
├── Frontend/
│   ├── client/src/
│   │   ├── pages/              # LiveMic, YouTube, FileTranscribe, TranscriptDetail, Settings
│   │   ├── components/ui/      # 50+ Radix UI Components
│   │   ├── hooks/              # use-backend-status, use-websocket, use-transcript-auto-refresh, use-toast
│   │   ├── lib/                # backend.ts (API-Client), queryClient.ts, utils.ts
│   │   ├── contexts/           # WebSocketContext, ThemeProvider
│   │   └── App.tsx, main.tsx, index.css
│   ├── server/                 # Express Dev/Prod Server
│   └── shared/                 # TypeScript Schemas
├── tests/                      # 21 pytest-Testdateien
├── docs/                       # Architektur- und Performance-Docs
├── downloads/                  # YouTube-Audio-Downloads
├── start.bat / start.sh        # Startup-Scripts
├── requirements.txt
├── pyproject.toml
└── README.md
```

---

## 14. Nicht-funktionale Anforderungen

- **Single-Instance:** Nur eine Scriber-Instanz gleichzeitig (Lock-File)
- **CORS:** Konfigurierbar, Default localhost-only
- **Security:** API-Keys in .env (nicht im Code), Datei-Upload mit Sanitisierung und Whitelist
- **Resilience:** Circuit Breaker, Retry mit Backoff, Job-Resume nach Restart
- **Observability:** Strukturierte Logs (loguru), Hot-Path-Metriken, Health-Endpoint
- **Responsiveness:** Echtzeit-WebSocket-Events, optimistic UI Updates, Lazy Loading
