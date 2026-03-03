# README-Analyse: Startpfade (Stand 2026-02-27)

## Windows (`start.bat`)
- Prüft Python, erstellt/aktiviert `venv`, installiert `requirements` bei Hash-Änderung.
- Legt `.env` bei Bedarf an.
- Wenn Node + `Frontend` verfügbar:
  - startet `python -m src.tray` im Hintergrund
  - wartet auf `http://127.0.0.1:8765/api/health`
  - öffnet Browser `http://localhost:5000`
- Ohne Node/Frontend: Fallback `python -m src.main` (Tkinter).

## Linux/macOS (`start.sh`)
- Führt ebenfalls Venv, Optional-Key-Prompt und startet `python -m src.main`.

## Direktaufrufe
- Backend: `python -m src.web_api`
- Tray: `python -m src.tray` (Backend via `python -m src.web_api` + Vite via `npm run dev:client`)
- Desktop Tk: `python -m src.main`
- Frontend: `cd Frontend && npm install && npm run dev:client` oder `npm run dev` für Express+Vite-Server.

## Konfigurations-Hinweis
- Backend-Host/Port default `127.0.0.1:8765`.
- `Frontend`-Port default `5000` via `npm run dev`/`dev:client`.
- CORS-Host-Liste via `SCRIBER_ALLOWED_ORIGINS`.
