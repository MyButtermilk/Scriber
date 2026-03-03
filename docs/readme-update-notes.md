# README Update Notes (2026-02-27)

## Letzter Stand der Analyse
- Backend-Endpunkte aus `src/web_api.py` verifiziert:
  - `GET /api/health`, `GET /api/state`, `GET /api/metrics/hot-path`
  - `GET /ws`
  - `POST /api/live-mic/{start,stop,toggle}`
  - Settings/Mikrofone/Autostart: `GET/PUT /api/settings`, `GET/POST /api/autostart`, `GET /api/microphones`
  - Transcript-Management: `GET /api/transcripts`, `GET/DELETE /api/transcripts/{id}`, `POST /api/transcripts/{id}/summarize`, `POST /api/transcripts/{id}/cancel`, `GET /api/transcripts/{id}/export/{format}`
  - YouTube: `GET /api/youtube/search`, `GET /api/youtube/video`, `POST /api/youtube/transcribe`
  - File: `POST /api/file/transcribe`
  - Lokale Modelle: ONNX (`/api/onnx/*`), NeMo (`/api/nemo/*`)
- Upload-Konstanten:
  - `SCRIBER_UPLOAD_MAX_MB` default 200 MB (Audio/after extraction)
  - `SCRIBER_UPLOAD_MAX_BYTES` optional harter Byte-Override
  - Video-Rohlimit hart auf 2048 MB (`_DEFAULT_VIDEO_MAX_MB`)
  - erlaubte Rohformate: `.mp3,.m4a,.wav,.ogg,.flac,.aac,.mp4,.mov,.webm,.avi,.mkv,.m4v`
  - Video-Erkennung für Extraktion: `.mp4,.mov,.webm,.avi,.mkv,.flv,.wmv,.m4v`
- CORS: `SCRIBER_ALLOWED_ORIGINS` (CSV) mit Standard nur localhost/127.0.0.1/::1.
- WebSocket sendet State + Events auf `'/ws'`; Events sind `state`/`status`/`transcript`/`audio_level`/`input_warning`/`transcribing`/`session_*`/`history_updated`/`error`.
- Wichtiger Hinweis: `summarize_transcript` nutzt intern noch Fallback `gemini-2.0-flash`, obwohl Config-Default `gemini-3-flash-preview` ist.

## Frontend-Verhalten
- Routen in `Frontend/client/src/App.tsx`: `/`, `/youtube`, `/file`, `/transcript/:id`, `/settings`.
- `apiUrl` defaultet auf `http://127.0.0.1:8765`.
- `VITE_BACKEND_URL` ist der override.
- Recorder-Hotkey-Modus mapping: UI `press_hold` ↔ Backend `push_to_talk`, `start_stop` ↔ `toggle`.

## Startpfade
- `start.bat`: prüft Python + `venv` + pip sync, erstellt `.env` bei Bedarf, startet bevorzugt Tray + Frontend auf `5000` + `/api/health`-Wait.
- `start.sh`: Linux/macOS, installiert Abhängigkeiten, startet `python -m src.main`.
- Manuelle Starts: `python -m src.web_api`, `python -m src.tray`, `python -m src.main`, `cd Frontend && npm run dev:client` oder `npm run dev`.

## Screenshots
- Erneuert in `docs/screenshots/*.png`:
  - `live_mic.png`, `youtube.png`, `file_upload.png`, `transcript_detail.png`, `settings.png`
- Standardgröße aktuell: 1600x2200

Stand: 2026-02-27 19:32:56
