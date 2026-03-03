# README-Analyse: Backend API (Stand 2026-02-27)

## Tatsächlich vorhandene Funktionen
- `src/web_api.py` bindet REST + WebSocket auf `127.0.0.1:8765` (konfigurierbar).
- CORS:
  - `SCRIBER_ALLOWED_ORIGINS` als CSV auswertet.
  - Leere Variable erlaubt standardmäßig nur `localhost`, `127.0.0.1`, `::1`.
  - `*` erlaubt alle Origins.
- Upload-Limits:
  - Standard Audio: `SCRIBER_UPLOAD_MAX_MB` (default `200` MB) + optional `SCRIBER_UPLOAD_MAX_BYTES`.
  - Video-Upload hard auf `2048` MB Rohlimit (`_DEFAULT_VIDEO_MAX_MB`).
  - Audio aus Video wird auf `200` MB begrenzt.
- Unterstützte Rohvideotype: `.mp4`, `.mov`, `.webm`, `.avi`, `.mkv`, `.flv`, `.wmv`, `.m4v`.

## Endpunkte laut code
- `GET /api/health`
- `GET /api/state`
- `GET /api/metrics/hot-path`
- `GET /ws`
- `POST /api/live-mic/start`
- `POST /api/live-mic/stop`
- `POST /api/live-mic/toggle`
- `GET /api/settings`
- `PUT /api/settings`
- `GET /api/autostart`
- `POST /api/autostart`
- `GET /api/microphones`
- `GET /api/transcripts?offset&limit&type&q`
- `GET /api/transcripts/{id}`
- `DELETE /api/transcripts/{id}`
- `POST /api/transcripts/{id}/summarize`
- `POST /api/transcripts/{id}/cancel`
- `GET /api/transcripts/{id}/export/{format}`
- `GET /api/youtube/search?q&maxResults&pageToken`
- `GET /api/youtube/video?id|url`
- `POST /api/youtube/transcribe`
- `POST /api/file/transcribe`
- ONNX: `GET /api/onnx/models`, `GET /api/onnx/models/{model_id}`, `POST /api/onnx/download`, `DELETE /api/onnx/models/{model_id}`
- NeMo: `GET /api/nemo/models`, `POST /api/nemo/download`, `DELETE /api/nemo/models/{model_id}`

## Wichtige Genauigkeiten
- `GET /api/transcripts` nutzt `type`=`mic|youtube|file`, `q`, `offset`, `limit` (Default `limit=50`, hart auf `1..100`).
- `POST /api/transcripts/{id}/summarize` nutzt intern bisher noch `getattr(Config, "SUMMARIZATION_MODEL", "gemini-2.0-flash")` statt konsistentem Default; eigentlicher Config-Default ist `gemini-3-flash-preview`.
- Settings API gibt `apiKeys` inkl.:
  - soniox, mistral, assemblyai, deepgram, openai, azureSpeechKey/Region, gladia, groq, speechmatics, elevenlabs, googleApiKey, googleApplicationCredentials, youtubeApiKey.
- AWS-Credentials werden in `PUT /api/settings` nicht vollständig geführt; AWS läuft über Standard-Umgebungsvariablen/SDK-Standard.

## Pipeline/Modus
- Provider-Routing via `ProviderRouter` und `ProviderCircuitBreaker` aktiv.
- Für Datei-/YouTube-Pfade vorhanden: JobStore, Retry-Mechanik (`RetryScheduler`) und Resume bei Neustart von Verarbeitungsjobs.
- `transcript`-Persistenz via SQLite (`db`).
