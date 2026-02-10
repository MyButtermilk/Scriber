# Performance Review (2026-02-06)

## Scope
- Backend: `src/pipeline.py`, `src/mistral_stt.py`, `src/web_api.py`, `src/database.py`, `src/microphone.py`
- Frontend: `Frontend/client/src/pages/LiveMic.tsx`, `Frontend/client/src/pages/FileTranscribe.tsx`, `Frontend/client/src/pages/TranscriptDetail.tsx`, `Frontend/client/src/contexts/WebSocketContext.tsx`

## Executive Summary
Der Code enthält bereits mehrere gute Optimierungen (lazy loading, WebSocket-Singleton, Broadcast-Throttling).  
Die größten verbleibenden Performance-Gewinne liegen bei:

1. Speicherverbrauch bei Datei-Transkription (große `read()`-Ladevorgänge in RAM)
2. CPU-Kosten beim Transcript-Listing/Searching
3. Frontend-Refetch-Stürme bei `history_updated`
4. O(n²)-Stringaufbau bei Live-Transkriptsegmenten

## Findings (priorisiert)

### P0: Große Dateien werden vollständig in RAM geladen
- Stelle:
  - `src/pipeline.py:1281`
  - `src/pipeline.py:1373`
- Problem:
  - `file_bytes = f.read()` lädt komplette Audio/Video-Dateien in den Speicher.
  - Bei großen Uploads (insb. Video) erzeugt das hohe RAM-Spitzen und mögliche OOM/GC-Last.
- Empfehlung:
  - Multipart-Upload streamen (Dateihandle statt Voll-Bytes).
  - Alternativ chunked Upload/`aiofiles` + streaming body.
- Erwarteter Impact:
  - Deutlich geringerer Peak-RAM (typisch 50-95% je Dateigröße).

### P0: Mistral Async puffert Audio unbounded im RAM
- Stelle:
  - `src/mistral_stt.py:304`
  - `src/mistral_stt.py:357`
  - `src/mistral_stt.py:379`
- Problem:
  - `self._buffer = bytearray()` wächst mit Aufnahmezeit.
  - Lange Sessions verursachen unnötig hohen Speicherverbrauch.
- Empfehlung:
  - Gleiches Muster wie Soniox nutzen: `tempfile.SpooledTemporaryFile`.
  - Optional `max_buffer_secs`/`max_buffer_bytes` hart begrenzen.
- Erwarteter Impact:
  - Stabilerer Speicherverbrauch bei langen Aufnahmen.

### P0: Transcript-Suche skaliert schlecht mit Datenmenge
- Stelle:
  - `src/web_api.py:1740`
  - `src/web_api.py:1764`
  - `src/web_api.py:1766`
  - `src/database.py` (kein FTS-Index)
- Problem:
  - Pro Request werden große Strings gebaut und `lower()`-konkateniert.
  - O(N * Textlänge) auf Python-Seite, trotz Pagination.
- Empfehlung:
  - Suche in SQLite verlagern (FTS5 für `title/content/summary/channel`).
  - Treffer bereits paginiert (`LIMIT/OFFSET`) aus DB liefern.
  - In-Memory-Filter nur als Fallback.
- Erwarteter Impact:
  - Große Beschleunigung bei vielen/langen Transkripten.

### P1: O(n²)-Aufbau von `content` bei Live-Segmenten
- Stelle:
  - `src/web_api.py:348`
  - `src/web_api.py:356`
- Problem:
  - Bei jedem Segment wird `"\n\n".join(self._segments)` neu berechnet.
  - Mit steigender Segmentzahl wächst die Gesamtkostenkurve überproportional.
- Empfehlung:
  - `content` inkrementell appenden (`self.content += ...`) statt komplettes Re-Join.
  - Alternativ Join nur bei `finish()` ausführen.
- Erwarteter Impact:
  - Niedrigere CPU-Last und weniger temporäre Allokationen in langen Sessions.

### P1: Preview-Berechnung splitet kompletten Content pro Listeneintrag
- Stelle:
  - `src/web_api.py:298`
  - `src/web_api.py:326`
- Problem:
  - `split()` auf vollem `content` je `to_public()` ist teuer.
  - Besonders relevant bei häufigen Listen-Refetches.
- Empfehlung:
  - Preview beim Persistieren/Append cachen (z. B. `preview` Feld).
  - In `to_public()` nur cached preview zurückgeben.
- Erwarteter Impact:
  - Spürbar schnellere Listen-Serialisierung.

### P1: Frontend refetched Listen zu aggressiv bei `history_updated`
- Stelle:
  - `Frontend/client/src/pages/FileTranscribe.tsx:229`
  - `Frontend/client/src/pages/FileTranscribe.tsx:230`
  - `Frontend/client/src/pages/FileTranscribe.tsx:223`
- Problem:
  - Bei jedem `history_updated` wird refetch auf Transcript-Queries ausgelöst.
  - Gleichzeitig `staleTime: 0` erhöht Netz- und Renderlast.
- Empfehlung:
  - Statt globalem Refetch: `setQueryData()` mit Delta-Updates (neues/aktualisiertes Item).
  - Wenn Refetch nötig: debounce/throttle clientseitig + gezielte Query-Key-Invalidierung.
  - `staleTime` für Listen anheben (z. B. 5-15s) bei WebSocket-getriebenen Updates.
- Erwarteter Impact:
  - Weniger Netzverkehr/Renderzyklen, glattere UI.

### P2: Doppelte Aktualisierung in TranscriptDetail (Polling + WebSocket)
- Stelle:
  - `Frontend/client/src/pages/TranscriptDetail.tsx:287`
  - `Frontend/client/src/pages/TranscriptDetail.tsx:314`
  - `Frontend/client/src/pages/TranscriptDetail.tsx:316`
- Problem:
  - Polling (`refetchInterval`) und WebSocket-Invalidierung laufen parallel.
  - Redundante Requests während `processing`.
- Empfehlung:
  - Entweder Polling oder WebSocket verwenden (bevorzugt WebSocket).
  - Fallback-Polling nur bei WS-Disconnect.
- Erwarteter Impact:
  - Niedrigere API-Last pro offenem Detail-Tab.

### P2: Audio-Level Broadcast-Frequenz kann bei mehreren Clients teuer werden
- Stelle:
  - `src/web_api.py:555`
  - `src/web_api.py:560`
  - `src/web_api.py:566`
- Problem:
  - ~60fps JSON-WebSocket-Events für `audio_level`.
- Empfehlung:
  - 20-30fps limitieren oder adaptive Rate (z. B. nur bei sichtbarer LiveMic-View hoch).
  - Payload komprimieren/quantisieren (z. B. `uint8` Level).
- Erwarteter Impact:
  - Weniger CPU/Netzwerk bei aktiver Aufnahme.

### P2: Startup-Init ist teilweise sequenziell und Mistral-Prewarm fehlt
- Stelle:
  - `src/web_api.py:2802`
  - `src/web_api.py:2813`
  - `src/web_api.py:2838`
  - `src/web_api.py:2855`
- Problem:
  - Trotz Kommentar „parallel“ laufen Teile sequenziell mit zusätzlichen Sleeps.
  - `_prewarm_stt_service` enthält noch keinen `mistral`-Branch.
- Empfehlung:
  - Nicht abhängige Prewarm-Schritte via `asyncio.gather`.
  - Mistral-Prewarm ergänzen.
- Erwarteter Impact:
  - Kürzere Startzeit bis zur ersten nutzbaren Aufnahme.

## Empfohlene Reihenfolge
1. P0: Streaming statt Voll-`read()` in `transcribe_file_direct`
2. P0: MistralAsync auf spooled buffer umstellen
3. P0/P1: Transcript-Suche auf SQLite FTS5 + serverseitige Pagination
4. P1: Inkrementeller `content`-Aufbau + cached preview
5. P1/P2: Frontend Refetch-Strategie auf Delta-Updates umstellen

## Messplan (vor/nach)
- Backend:
  - Peak RAM während 1GB Upload
  - P95 Latenz `/api/transcripts?q=...` bei 1k+ Einträgen
  - WebSocket msg/s während Live-Aufnahme
- Frontend:
  - Requests/min bei laufender Transkription (File/YouTube/Detail)
  - Commit/Render-Zeit in React Profiler für Listenansichten

## Quick Wins (geringer Aufwand)
- `src/web_api.py:356` Join-Strategie ersetzen (inkrementell)
- `Frontend/client/src/pages/TranscriptDetail.tsx` Polling bei WS-Verbindung deaktivieren
- `src/web_api.py` `audio_level` von 60fps auf 30fps reduzieren
