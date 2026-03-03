# README-Analyse: Frontend API (Stand 2026-02-27)

## Routing / Navigationsstruktur
- Wouter-Routing in `Frontend/client/src/App.tsx`.
- Routen:
  - `/` => Live Mic
  - `/youtube` => YouTube
  - `/file` => File Transcribe
  - `/transcript/:id` => Transcript Detail
  - `/settings` => Settings
  - Catch-all -> Not Found

## Architektur-Frontendentwicklung
- Backend-Konfiguration:
  - `apiUrl()` nutzt `VITE_BACKEND_URL`, Standard `http://127.0.0.1:8765`.
  - `wsUrl()` nutzt denselben Base und `ws/wss`-Umwandlung.
- State/Events:
  - Globaler `WebSocketProvider` auf `/ws`.
  - Transkriptlisten mit React Query + Auto-Refresh Hook.

## Feature-Check gegen Code
- Live Mic Page zeigt Liste (Typ `mic`) + Live-Kontrolle, Mikrofon-Status und visuelle Audioanzeige.
- YouTube Page nutzt `/api/youtube/search` + `/api/youtube/transcribe` und zeigt Such-/Verarbeitungsstatus.
- File Upload Page nutzt `POST /api/file/transcribe` via `react-dropzone` und `FormData(file)`.
- Transcript-Detail Page zeigt Export/Summary/Stop-Funktionalität (`/api/transcripts/{id}/cancel`, `/summarize`, `/export/{format}`).
- Settings Page kann Provider-Auswahl, Hotkey, Modus, Sprache, Injection, ONNX/NeMo verwalten.

## Defaults / Verhalten aus Settings-Seite
- Visualizer Bar Count default UI-Value aktuell `45`.
- STT-Model-Labels in UI enthalten `soniox-...`, `mistral`, `assemblyai`, `deepgram`, `openai`, `azure`, `gladia`, `groq`, `speechmatics`, `elevenlabs`, `google`, `aws` sowie lokale ONNX/NeMo.
