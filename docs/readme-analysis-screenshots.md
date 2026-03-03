# README-Analyse: Screenshot-Plan (Stand 2026-02-27)

## Ziel
Playwriter wird zur Generierung der fünf README-Screenshots genutzt:
- `docs/screenshots/live_mic.png`
- `docs/screenshots/youtube.png`
- `docs/screenshots/file_upload.png`
- `docs/screenshots/transcript_detail.png`
- `docs/screenshots/settings.png`

## Beobachtete Routen
- `/`
- `/youtube`
- `/file`
- `/transcript/:id`
- `/settings`

## Vorgehen
1. Playwriter-Page auf `http://localhost:5000` öffnen.
2. Auf jede Zielseite navigieren.
3. Zustand so setzen, dass die Seite sinnvoll gefüllt ist (z. B. Platzhalter/Listenansicht statt leere Fehlzustände).
4. Vollbildnaher Screenshot pro Seite direkt als PNG in den genannten Dateien schreiben.
