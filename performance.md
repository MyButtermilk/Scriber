# Performance-Analyse (revalidiert + erweitert)

Stand: 2026-02-27

Vierter Full-Review: Alle Aussagen wurden erneut gegen den aktuellen Code geprüft. Falsche Annahmen wurden korrigiert, neue valide Bottlenecks ergänzt.

## Kurzfazit

Die größten realen Hebel sind:

1. Unnötige `json.dumps`-Serialisierung bei ~30fps im WS-Broadcast, auch ohne Clients.
2. Blockierende Dateischreibzugriffe im Upload-Request.
3. O(n²) String-Concatenation im Live-Transkript-Pfad.
4. Settings-Slider feuert `PUT` + `.env`-Vollwrite pro Tick.
5. Export (`PDF`/`DOCX`) läuft synchron im async Handler und blockiert den Event-Loop.
6. `history_updated` + Detail-Refetch kann bei laufenden Jobs unnötig hohe Refetch-Frequenz erzeugen.

## Revalidierung der bisherigen Aussagen

### Valide (erneut bestätigt)

| Aussage | Codestelle |
|---|---|
| Blockierendes File-Write im Upload-Handler | `src/web_api.py:3584`, `src/web_api.py:3593` |
| 1-Sekunden-Re-Render im LiveMic Parent | `Frontend/client/src/pages/LiveMic.tsx:454` |
| N+1-Existenzchecks im Suchpfad | `src/web_api.py:3116` |
| CORS-Origins werden pro Request neu geparst | `src/web_api.py:163-179` |
| O(n²)-Charakter bei `content`-Stringaufbau | `src/web_api.py:538-539` |
| Wiederholtes Datumsparsing in `to_public()` | `src/web_api.py:482-485` |
| Job-/Metrics-Stores öffnen pro Operation neue DB-Connection | `src/data/job_store.py:88`, `src/data/latency_metrics_store.py:31` |
| `use-toast` Effect hängt von `[state]` ab | `Frontend/client/src/hooks/use-toast.ts:174-182` |

### Korrigiert / relativiert

- **`ffprobe` blockiert nicht den Event-Loop direkt:**
  - `_probe_media_duration_seconds()` ist synchron (`src/web_api.py:327`), wird aber über `asyncio.to_thread(...)` aufgerufen (`src/web_api.py:1882`).

- **"SQLite save pro Segment im Live-Hotpath" als pauschale Aussage ist falsch:**
  - Persistierung passiert primär bei Abschluss-/Statuspfaden, nicht bei jedem Interim/Final-Chunk.

- **`staleTime: Infinity` ist kein genereller Performance-Bug:**
  - Mit WS-getriebener Invalidierung ist das in diesem Setup sinnvoll und reduziert Background-Fetches.

- **`invalidateQueries + refetchQueries` in `use-transcript-auto-refresh` ist hier korrekt:**
  - Bei `staleTime: Infinity` reicht reines Invalidate nicht für aktive Queries.
  - Referenz: `Frontend/client/src/hooks/use-transcript-auto-refresh.ts:28-29`, `:35-36`.

- **Response-Body wird in `request-errors.ts` nicht doppelt gelesen:**
  - JSON- und Text-Pfad sind content-type-abhängige Alternativen.
  - Referenz: `Frontend/client/src/lib/request-errors.ts:59-71`.

- **Soniox-Polling ist nur teilweise bereits optimiert:**
  - Adaptive Backoff existiert im Soniox-Async-Pfad (`src/pipeline.py:302-324`).
  - Im Soniox-Direct-File-Pfad gibt es weiterhin statisches Polling (`src/pipeline.py:1544`).

- **Breite Invalidierungen wurden teilweise bereits verbessert:**
  - `FileTranscribe` und `Youtube` verwenden Predicate-Filter (`Frontend/client/src/pages/FileTranscribe.tsx:344-347`, `Frontend/client/src/pages/Youtube.tsx:447-451`, `:491-494`).
  - Breite Invalidierung bleibt u. a. in `LiveMic`/`TranscriptDetail` (`Frontend/client/src/pages/LiveMic.tsx:646`, `Frontend/client/src/pages/TranscriptDetail.tsx:434`).

## P0 (höchster Impact)

### P0-1: `audio_level` erzeugt unnötige Arbeit ohne Clients
- **Befund:**
  - `_on_audio_level` läuft mit ~30fps (`src/web_api.py:1377-1390`).
  - Jeder Tick plant Task + Broadcast (`src/web_api.py:1388-1389`), auch ohne Clients.
  - `broadcast()` serialisiert vor Client-Check (`src/web_api.py:1248`, `:1255`).
- **Verbesserung:**
  - Early-Return in `_on_audio_level`, wenn keine Clients.
  - In `broadcast()` zuerst Client-Check, dann `json.dumps`.
  - Optional Coalescing pro Event-Loop-Tick.

### P0-2: Upload schreibt synchron im async Request
- **Befund:**
  - `f.write(chunk)` ist synchron im async Handler (`src/web_api.py:3593`).
  - Bei großen Uploads blockiert das den Event-Loop.
- **Verbesserung:**
  - Write-Pfad in Thread/Executor auslagern (`asyncio.to_thread` o. ä.).

### P0-3: O(n²)-Concatenation im Live-Transkript
- **Befund:**
  - `self.content = f"{self.content}\n\n{cleaned}"` (`src/web_api.py:539`).
  - Jeder Append kopiert den bisherigen Gesamtstring.
- **Verbesserung:**
  - Intern `list[str]` sammeln und am Ende `"\n\n".join(...)`.

### P0-4: Settings-Slider verursacht Request- und Disk-Sturm
- **Befund:**
  - `onValueChange` ruft `updateSettings()` pro Tick (`Frontend/client/src/pages/Settings.tsx:1796`, `:848-853`).
  - Jeder Request persistiert komplette `.env` (`src/web_api.py:2916`, `src/config.py:332-333`).
- **Verbesserung:**
  - Persistenz auf `onValueCommit` oder Debounce umstellen.
  - Backend-seitiges Write-Coalescing ergänzen.

## P1 (mittlerer Impact)

### P1-1: Export blockiert den Event-Loop (sync CPU/IO im async Handler)
- **Befund:**
  - `export_transcript` ist async (`src/web_api.py:3740`), ruft aber synchron `export_to_pdf`/`export_to_docx` (`src/web_api.py:3771-3787`).
  - Exportfunktionen bauen Dokumente synchron in Python (`src/export.py:120-215`, `src/export.py:218-382`).
- **Verbesserung:**
  - Exportgenerierung in `asyncio.to_thread(...)` auslagern.

### P1-2: Hohe `history_updated`-Frequenz kann Detail-Refetch-Sturm auslösen
- **Befund:**
  - Backend throttelt global auf 250ms (`src/web_api.py:660`, `src/web_api.py:1499-1514`).
  - Detail-View reagiert auf `history_updated` mit debounced Refresh alle 250ms (`Frontend/client/src/hooks/use-transcript-auto-refresh.ts:19`, `:63-70`, `:28-29`).
  - Detail-Fetch lädt vollständiges Transcript (`src/web_api.py:3416-3422`), nicht nur Delta/Status.
- **Verbesserung:**
  - Für Detail-Views niedrigere Refresh-Frequenz (z. B. 750-1000ms) oder Delta-Events.
  - Optional detail-spezifische WS-Payloads (`step`, `status`, `deltaText`) statt Full-Refetch.

### P1-3: Transcript-Filterung skaliert linear über komplette History
- **Befund:**
  - Typ-Filter baut vollständiges `filtered`, dann pagination (`src/web_api.py:3162-3174`).
- **Verbesserung:**
  - Streaming-Filter mit Offset/Limit-Counter oder Typ-Index.

### P1-4: `use-toast` re-subscribed bei jeder State-Änderung
- **Befund:**
  - Effect ist an `[state]` gebunden (`Frontend/client/src/hooks/use-toast.ts:174-182`).
- **Verbesserung:**
  - Einmalige Subscription mit `[]`.

### P1-5: CORS-Origins werden pro Request neu geparst
- **Befund:**
  - `_parse_allowed_origins()` in jedem `_origin_allowed()`-Call (`src/web_api.py:163-179`).
- **Verbesserung:**
  - Geparste Origins cachen; nur bei Settings-Änderung refreshen.

### P1-6: Breite Invalidierungen in LiveMic/Detail
- **Befund:**
  - `LiveMic.tsx:646` und `TranscriptDetail.tsx:434` invalidieren pauschal `/api/transcripts`.
- **Verbesserung:**
  - Präzisere Keys oder Predicate-Filter.

### P1-7: Wiederholtes Datumsparsing in `to_public()`
- **Befund:**
  - `datetime.fromisoformat` pro Serialisierung (`src/web_api.py:482-485`).
- **Verbesserung:**
  - Label-Cache (z. B. bei Tageswechsel invalidieren).

### P1-8: N+1-Existenzcheck im Suchpfad
- **Befund:**
  - Pro Match `db.transcript_exists` (`src/web_api.py:3116`, `src/database.py:319-327`).
- **Verbesserung:**
  - In-Memory Set der persistierten IDs oder Batch-Existenzcheck.

### P1-9: Synchronous SQLite Writes in async Pfaden
- **Befund:**
  - Async Controller ruft synchrones `_save_transcript_to_db()` auf (`src/web_api.py:1173-1176`, z. B. `:2539`).
  - `db.save_transcript()` führt synchrones SQL + FTS-Sync aus (`src/database.py:165-198`, `src/database.py:53-63`).
- **Verbesserung:**
  - DB-Writes in `to_thread`/dedizierten Writer-Task auslagern.

### P1-10: Job-/Metrics-Stores öffnen pro Aufruf neue Connection
- **Befund:**
  - `JobStore._connect()` / `LatencyMetricsStore._connect()` öffnen je Operation neue SQLite-Connection inkl. PRAGMA.
  - Referenzen: `src/data/job_store.py:88-93`, `src/data/latency_metrics_store.py:31-36`.
- **Verbesserung:**
  - Thread-lokale Reuse analog `src/database.py`.

## P2 (optional / langfristig)

### P2-1: LiveMic Parent-Timer kann stärker isoliert werden
- **Befund:**
  - `setElapsed` im Parent tickt 1/s (`Frontend/client/src/pages/LiveMic.tsx:454`).
- **Verbesserung:**
  - Isolierte Timer-Subkomponente.

### P2-2: PTT pollt alle 50ms
- **Befund:**
  - `await asyncio.sleep(0.05)` (`src/web_api.py:2644-2658`).
- **Verbesserung:**
  - Event-basierte Hooks bevorzugen; Polling als Fallback.

### P2-3: Fixe Sleeps im Pipeline-Stop
- **Befund:**
  - Feste Sleeps/Wait-Loops (`src/pipeline.py:1619`).
- **Verbesserung:**
  - Event-/Queue-getriebenes Drain-Handling.

### P2-4: Soniox Direct File Polling ist statisch
- **Befund:**
  - Poll-Loop mit `await asyncio.sleep(1)` (`src/pipeline.py:1544`).
- **Verbesserung:**
  - Adaptive Delays (abhängig von Laufzeit/Status), analog zum bereits optimierten Async-Pfad.

### P2-5: Hover-Prefetch ohne Intent-Delay
- **Befund:**
  - Sofortiger Prefetch auf Hover (`Frontend/client/src/pages/LiveMic.tsx:708`, `Frontend/client/src/pages/FileTranscribe.tsx:404`, `Frontend/client/src/pages/Youtube.tsx:551`).
- **Verbesserung:**
  - 100-150ms Intent-Delay und Cache-Check vor Prefetch.

### P2-6: WebSocketContext-`value` ohne `useMemo`
- **Befund:**
  - `value`-Objekt wird pro Render neu erstellt (`Frontend/client/src/contexts/WebSocketContext.tsx:152-157`).
- **Verbesserung:**
  - `useMemo` für stabileres Context-Value-Objekt.

## Empfohlene Umsetzungsreihenfolge

| Prio | Issue | Aufwand | Effekt |
|---|---|---|---|
| 1 | P0-1 | 15 min | unnötige 30fps WS-Serialization/Task-Overhead eliminieren |
| 2 | P0-3 | 20 min | O(n²) → O(n) im Live-Append-Pfad |
| 3 | P0-2 | 15 min | Event-Loop-Blockade bei Upload reduzieren |
| 4 | P0-4 | 10 min | Slider-Sturm und `.env`-Write-Sturm beseitigen |
| 5 | P1-1 | 20 min | Export blockiert Event-Loop nicht mehr |
| 6 | P1-2 | 20 min | Detail-Refetch-Last während laufender Jobs reduzieren |
| 7 | P1-4 | 5 min | Toast-Listener-Churn stoppen |
| 8 | P1-10 | 20 min | DB-Connection-Overhead in Job/Metrics-Stores senken |

## Messplan (vorher/nachher)

### Backend
- **WS ohne offene UI:** CPU% + Task-Count im Recording vor/nach P0-1.
- **Upload unter Last:** P95 von parallelen `GET /api/state` während großem Upload.
- **Settings-Burst:** Zahl der `PUT /api/settings` + `.env`-Writes bei 3s Slider-Drag.
- **Export:** P95 von `/api/state` während parallel 2-3 Exports.

### Frontend
- **Detail-Refetchrate:** Requests/min auf `/api/transcripts/:id` während aktiver Verarbeitung.
- **React Profiler:** Commit-Zeit/Rerenders für `LiveMic`, `TranscriptDetail`, `Settings`.
- **Toast-Listener:** Anzahl `listeners.push/splice` vor/nach P1-4.

## Zusätzliche Feature-Ideen mit Performance-Nutzen

1. WS-Diff-Events (`transcript_upsert`, `transcript_deleted`, `transcript_progress`) statt globalem `history_updated`.
2. Incremental List Mode (Cursor/Infinite Query) für sehr große Histories.
3. Write-behind Queue für nicht-kritische Persistenzpfade (`.env`, sekundäre Statusupdates).
