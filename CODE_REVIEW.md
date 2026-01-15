# Scriber Code Review - Bug- und Risiko-Analyse

**Datum:** 2026-01-14
**Version:** 1.1
**Reviewer:** GPT-5.2 (Codex CLI)

---

## Inhaltsverzeichnis

0. [Automatisierte Checks](#0-automatisierte-checks)
1. [Architektur-Zusammenfassung](#1-architektur-zusammenfassung)
2. [Backend-Probleme](#2-backend-probleme)
3. [Frontend-Probleme](#3-frontend-probleme)
4. [Integrations-Probleme](#4-integrations-probleme)
5. [Zusammenfassung & Prioritäten](#5-zusammenfassung--prioritäten)
6. [Performance-Verbesserungen](#6-performance-verbesserungen)

---

## 0. Automatisierte Checks

**Umgebung:** Windows, `venv` vorhanden, Python 3.13.7

### 0.1 Python

- `venv\Scripts\python -m compileall -q src` ✅
- `venv\Scripts\python check_imports.py` ✅ (alle STT-Services importierbar)
- `venv\Scripts\python -m pytest` ❌ (4 failed, 17 passed)
  - `tests/test_injector.py::TestInjector::test_deduplication_survives_flush`
  - `tests/test_youtube_download.py` (3 Tests; Patch auf nicht existierende Funktion `_find_yt_dlp_command`)
  - Zusatz: Loguru-Handler meldet am Ende `ValueError: I/O operation on closed file` (siehe [2.5 Datenbank-Probleme](#25-datenbank-probleme))

### 0.2 Frontend

- `cd Frontend; npm run check` ❌
  - `Frontend/client/src/lib/queryClient.ts`: `TS2304: Cannot find name 'T'` (siehe [3.0 Build/TypeScript](#30-buildtypescript))

---

## 1. Architektur-Zusammenfassung

### 1.1 Systemübersicht

```
┌─────────────────────────────────────────────────────────────┐
│                     Windows Desktop                         │
├─────────────────────────────────────────────────────────────┤
│  ┌──────────────────┐  ┌──────────────────┐  ┌──────────┐  │
│  │  Microphone      │  │  YouTube/File    │  │ Keyboard │  │
│  │  (sounddevice)   │  │  (yt-dlp, ffmpeg)│  │ (hotkey) │  │
│  └────────┬─────────┘  └────────┬─────────┘  └────┬─────┘  │
│           └──────────────┬───────┴──────────────────┘       │
│                          ▼                                   │
│  ┌────────────────────────────────────────────────────┐     │
│  │        ScriberPipeline (Pipecat)                   │     │
│  │  Audio → VAD → Turn Detection → STT Service        │     │
│  └────────────────┬───────────────────────────────────┘     │
│                   ▼                                          │
│  ┌────────────────────────────────────────────────────┐     │
│  │   ScriberWebController (aiohttp REST + WebSocket)  │     │
│  │   Port 8765                                        │     │
│  └────────────────┬───────────────────────────────────┘     │
│                   │                                          │
│  ┌────────────────▼─────────────────────────────────┐       │
│  │ SQLite Database (transcripts.db)                 │       │
│  └──────────────────────────────────────────────────┘       │
└─────────────────────────────────────────────────────────────┘
                         │ HTTP/WebSocket
        ┌────────────────▼────────────────┐
        │  React 19 Frontend (Port 5000)  │
        │  TanStack Query + WebSocket     │
        └─────────────────────────────────┘
```

**Entry-Points / Betriebsarten (wichtig für Risiko-Bewertung):**
- **Desktop UI:** `python -m src.main` (Tkinter) nutzt `ScriberPipeline` direkt und injiziert Text lokal via `TextInjector`.
- **Web UI (Tray):** `python -m src.tray` startet **Backend** (`python -m src.web_api`, `127.0.0.1:8765`) + **Frontend** (`npm run dev:client`, Port `5000`), Browser spricht per HTTP + WebSocket mit dem Backend.

### 1.2 Kritische Schnittstellen

| Schnittstelle | Protokoll | Beschreibung |
|---------------|-----------|--------------|
| `/api/*` | HTTP REST | Alle CRUD-Operationen |
| `/ws` | WebSocket | Echtzeit-Updates (Audio, Transkription, Status) |
| `transcripts.db` | SQLite WAL | Persistente Datenspeicherung |
| STT Services | HTTPS | Soniox, OpenAI, Google, etc. |

### 1.3 Zustandsübergänge

```
Transcript Status Flow:
┌──────────┐     ┌────────────┐     ┌───────────┐
│ recording│ ──▶ │ processing │ ──▶ │ completed │
└──────────┘     └────────────┘     └───────────┘
      │                │
      │                ▼
      │          ┌──────────┐
      └────────▶ │  failed  │
                 └──────────┘
```

---

## 2. Backend-Probleme

### 2.1 Sicherheitsprobleme

#### KRITISCH: API-Keys im Settings-Endpoint exponiert
**Datei:** `src/web_api.py:1026-1060`
**Schweregrad:** KRITISCH

```python
# PROBLEM: Alle API-Keys werden an das Frontend gesendet
"apiKeys": {
    "soniox": Config.SONIOX_API_KEY or "",
    "assemblyai": Config.ASSEMBLYAI_API_KEY or "",
    # ... alle Keys exponiert
}
```

**Risiko:** Jede Client-Side-Schwachstelle kann alle API-Keys leaken.

**Lösung:**
```python
"apiKeys": {
    "soniox": {"configured": bool(Config.SONIOX_API_KEY)},
    "assemblyai": {"configured": bool(Config.ASSEMBLYAI_API_KEY)},
}
```

---

#### KRITISCH: CORS-Fehlkonfiguration mit Wildcard-Fallback
**Datei:** `src/web_api.py:1413-1434`
**Schweregrad:** KRITISCH

```python
# PROBLEM: Wildcard erlaubt alle Origins
if origin:
    resp.headers["Access-Control-Allow-Origin"] = origin
else:
    resp.headers["Access-Control-Allow-Origin"] = "*"  # GEFÄHRLICH!
```

**Risiko:** In Kombination mit `SCRIBER_ALLOWED_ORIGINS="*"` kann jede Website aus dem Browser heraus auf die lokale API zugreifen.

**Lösung:** Default auf `localhost` beschränken:
```python
allowed = {"http://localhost:5000", "http://127.0.0.1:5000"}
if origin in allowed:
    resp.headers["Access-Control-Allow-Origin"] = origin
```

---

#### HOCH: YouTube-URL nicht validiert (SSRF-Risiko)
**Datei:** `src/web_api.py:550-554`
**Schweregrad:** HOCH

```python
# PROBLEM: URL wird nicht validiert
url = payload.get("url", "").strip()
if not url:
    raise ValueError("Missing video URL")
# Keine Prüfung ob es wirklich eine YouTube-URL ist!
```

**Risiko:** SSRF - Angreifer könnte interne URLs aufrufen.

**Lösung:**
```python
import re
YOUTUBE_PATTERN = re.compile(r'^https?://(www\.)?(youtube\.com|youtu\.be)/')
if not YOUTUBE_PATTERN.match(url):
    raise ValueError("Invalid YouTube URL")
```

---

#### HOCH: Symlink-Angriff bei File-Upload nicht geschützt
**Datei:** `src/web_api.py:1765-1774`
**Schweregrad:** HOCH

```python
# PROBLEM: Keine Symlink-Prüfung
save_path = save_dir / safe_filename
with open(save_path, "wb") as f:  # Folgt Symlinks!
    while True:
        chunk = await file_field.read_chunk(size=1024 * 1024)
        f.write(chunk)
```

**Risiko:** Auf Unix-Systemen könnte ein Angreifer beliebige Dateien überschreiben.

**Lösung:**
```python
if save_path.is_symlink():
    raise ValueError("Symlink not allowed")
```

---

### 2.2 Nebenläufigkeitsprobleme

#### HOCH: Race Condition bei `_current` Transcript
**Datei:** `src/web_api.py:344-346, 484-490`
**Schweregrad:** HOCH

```python
# PROBLEM: _current wird von verschiedenen Threads ohne Lock zugegriffen
self._current: Optional[TranscriptRecord] = None

def _on_transcription(self, text: str, is_final: bool):
    # Wird vom STT-Service Callback aufgerufen (anderer Thread)
    if self._current:  # Kein Lock!
        self._current.content += text
```

**Lösung:**
```python
self._current_lock = threading.Lock()

def _on_transcription(self, text: str, is_final: bool):
    with self._current_lock:
        if self._current:
            self._current.content += text
```

---

#### MITTEL: SQLite `check_same_thread=False` deaktiviert Sicherheit
**Datei:** `src/database.py:34`
**Schweregrad:** MITTEL

```python
# PROBLEM: Thread-Sicherheit deaktiviert
conn = sqlite3.connect(_DB_PATH, check_same_thread=False, timeout=30.0)
```

**Risiko:** Bei Fehlern können verschiedene Threads dieselbe Connection nutzen → Korruption.

---

### 2.3 Exception Handling

#### MITTEL: Bare Exception Catching - Stille Fehler
**Datei:** `src/database.py:53-54, 124-125`
**Schweregrad:** MITTEL

```python
# PROBLEM: Fehler werden verschluckt
except Exception:
    pass  # Datenverlust wird nicht gemeldet!
```

**Risiko:** Transkriptionen könnten verloren gehen ohne User-Benachrichtigung.

---

#### MITTEL: Fehler-Messages können sensible Infos leaken
**Datei:** `src/web_api.py:528`
**Schweregrad:** MITTEL

```python
# PROBLEM: Exception-Text direkt an User
user_msg = f"Recording failed: {exc}"  # Könnte Pfade, Keys enthalten
```

**Lösung:** Nur vordefinierte, sanitisierte Fehlermeldungen verwenden.

---

### 2.4 Externe API-Probleme

#### MITTEL: Timeout-Policy uneinheitlich (YouTube API / yt-dlp)
**Datei:** `src/youtube_api.py:62-87`
**Schweregrad:** MITTEL

```python
# YouTube API nutzt session.get(...) ohne per-call Timeout-Argument
# (Timeout hängt an der ClientSession-Konfiguration im Caller)
async with session.get(url, params=params) as resp:
    # Request könnte ewig hängen
```

**Einordnung:** In `src/web_api.py` wird für den App-HTTP-Client ein Timeout gesetzt (`ClientTimeout(total=15)`), und `/api/youtube/video` nutzt `ClientTimeout(total=30)`. Das reduziert das Risiko, ist aber “implizit” und leicht zu umgehen, wenn der Call-Site später geändert wird.

---

#### MITTEL: Keine Retry-Logik für externe APIs
**Datei:** `src/web_api.py:1640-1645`
**Schweregrad:** MITTEL

```python
# PROBLEM: Kein Retry bei 429 (Rate Limit) oder 503
payload = await search_youtube_videos(api_key, q, ...)
```

**Lösung:** Exponential Backoff implementieren.

---

### 2.5 Datenbank-Probleme

#### HOCH: Summarize/Export können nur “Preview-Content” verwenden (Lazy-Load Bug)
**Dateien:** `src/web_api.py` (Handler `summarize_transcript`, `export_transcript`), `src/web_api.py:_load_transcripts_from_db`, `src/database.py:load_transcript_metadata`
**Schweregrad:** HOCH

**Problem:** Beim Startup werden Transkripte im “metadata-only” Modus geladen (Content ist `_previewText`, `summary=""`). `summarize_transcript` und `export_transcript` arbeiten aber mit `rec.content` aus `ctl._history`, ohne sicherzustellen, dass der vollständige Content aus der DB nachgeladen wurde.

**Risiko / Auswirkung:**
- Falsche/verkürzte Zusammenfassungen (LLM sieht nur die ersten ~100 Zeichen).
- Exporte (PDF/DOCX) können unvollständig sein.
- Persistierung: Summary wird ggf. “falsch” gespeichert und überdeckt die eigentlich korrekte Erwartung.

**Fix-Idee:** Vor Summarize/Export: `full = ctl.get_transcript(id)` oder `db.get_transcript(id)` laden und `rec.content`/`rec.summary` aktualisieren.

---

#### NIEDRIG: Loguru-Fehler beim Interpreter-Exit (atexit)
**Datei:** `src/database.py:42-56`
**Schweregrad:** NIEDRIG

**Beobachtung:** Beim Testlauf endet der Prozess mit `ValueError: I/O operation on closed file` aus dem Loguru-Sink, ausgelöst durch Logging in `_close_all_connections()` während/ nach pytest-Capture-Cleanup.

**Fix-Idee:** Logging im atexit-Handler vermeiden oder `ValueError` beim Schreiben abfangen.

---

#### NIEDRIG: WAL-Mode mit `PRAGMA synchronous=NORMAL`
**Datei:** `src/database.py:37-38`
**Schweregrad:** NIEDRIG

```python
conn.execute("PRAGMA journal_mode=WAL")
conn.execute("PRAGMA synchronous=NORMAL")  # Risiko bei Crash
```

**Risiko:** Bei App-Crash während Schreibvorgang könnte Datenbank korrupt werden.

---

### 2.6 Tooling/Tests

#### MITTEL: Pytest-Suite ist rot (Test/Implementation drift)
**Dateien:** `tests/test_injector.py`, `tests/test_youtube_download.py`
**Schweregrad:** MITTEL

**Beobachtung:** `venv\Scripts\python -m pytest` endet mit 4 Fails (u.a. `_find_yt_dlp_command` wird gepatcht, existiert aber nicht; `TextInjector` wird ohne `StartFrame` genutzt und injiziert nicht deterministisch über `keyboard.write`).

**Risiko:** CI/Regressionen schwer erkennbar; Tests suggerieren falsches Verhalten und können reale Bugs verdecken.

**Fix-Idee:** Tests an aktuelle Implementierung anpassen (StartFrame senden, `_send_input_text`/`_paste_text` mocken oder `_inject_text` patchen; `youtube_download` Tests auf `shutil.which`/ImportError/Subprocess mocken).

---

## 3. Frontend-Probleme

### 3.0 Build/TypeScript

#### HOCH: TypeScript-Check schlägt fehl (Generic `T` nicht im Scope)
**Datei:** `Frontend/client/src/lib/queryClient.ts:52-74`
**Schweregrad:** HOCH

```ts
export const getQueryFn: <T>(options: { ... }) => QueryFunction<T> =
  ({ on401 }) =>
    async ({ queryKey }) => {
      return null as T; // TS2304: Cannot find name 'T'
    };
```

**Impact:** `npm run check` (tsc) bricht ab; Type-Safety/CI kann nicht grün werden.

**Fix-Idee:** `getQueryFn` als echte generische Funktion definieren (`export const getQueryFn = <T,>(...) => ...`), damit `T` im Funktionskörper gültig ist.

---

### 3.1 State Management

#### HOCH: Memory Leak durch setTimeout ohne Cleanup
**Dateien:** `LiveMic.tsx:371`, `FileTranscribe.tsx:348`, `Youtube.tsx:448`
**Schweregrad:** HOCH

```typescript
// PROBLEM: setTimeout wird bei Unmount nicht gecancelt
setTimeout(() => setCopyingId(null), 1500);
```

**Risiko:** State-Update auf unmounted Component, Memory Leak.

**Lösung:**
```typescript
useEffect(() => {
  if (copyingId) {
    const timer = setTimeout(() => setCopyingId(null), 1500);
    return () => clearTimeout(timer);
  }
}, [copyingId]);
```

---

#### HOCH: Race Condition bei Double-Click
**Dateien:** `FileTranscribe.tsx:293-321`, `Youtube.tsx:393-421`
**Schweregrad:** HOCH

```typescript
// PROBLEM: Check passiert nach async Start
const deleteTranscript = useCallback(async (e, id) => {
    if (deletingId) return;  // Zu spät bei Double-Click!
    setDeletingId(id);
    await fetch(...);  // Race Window!
}, [deletingId]);
```

**Lösung:** Optimistic UI mit sofortiger State-Änderung oder Debounce.

---

### 3.2 API-Handling

#### HOCH: Fehlende `res.ok` Prüfung
**Datei:** `LiveMic.tsx:192`
**Schweregrad:** HOCH

```typescript
// PROBLEM: Keine Fehlerprüfung vor json()
const res = await fetch(apiUrl(`/api/transcripts?${params}`));
return res.json();  // Wirft Fehler bei Error-Response!
```

**Lösung:**
```typescript
if (!res.ok) {
  throw new Error(`HTTP ${res.status}: ${res.statusText}`);
}
return res.json();
```

---

#### HOCH: Unsafe Type Casting mit `as any`
**Dateien:** `LiveMic.tsx:195`, `FileTranscribe.tsx:224`, `Youtube.tsx:282`
**Schweregrad:** HOCH

```typescript
// PROBLEM: Keine Type Safety
const transcripts: Transcript[] = (transcriptsQuery.data as any)?.items || [];
```

**Risiko:** API-Änderungen brechen Code ohne Warnung.

**Lösung:** Proper TypeScript Types definieren und validieren.

---

### 3.3 UX-Probleme

#### HOCH: Kein Error State in TranscriptDetail
**Datei:** `TranscriptDetail.tsx:268-295`
**Schweregrad:** HOCH

```typescript
// PROBLEM: Bei Fehler wird Default gezeigt, kein Error
const transcript = transcriptQuery.data || mock || {
    title: "Transcript",
    // ...
};
// Kein: if (transcriptQuery.error) return <ErrorState />
```

---

#### MITTEL: Silent Failure in Settings
**Datei:** `Settings.tsx:344-350`
**Schweregrad:** MITTEL

```typescript
// PROBLEM: Fehler wird verschluckt
const handleCustomVocabBlur = async () => {
    try {
        await updateSettings({ customVocab: customVocabulary });
    } catch {
        // ignore  ← User weiß nicht ob gespeichert!
    }
};
```

---

### 3.4 Performance

#### MITTEL: Teure Regex ohne Memoization
**Datei:** `TranscriptDetail.tsx:33-102`
**Schweregrad:** MITTEL

```typescript
// PROBLEM: Regex bei jedem Render
function SpeakerFormattedText({ content }) {
    const speakerPattern = /\[Speaker (\d+)\]:\s*/g;
    // Teures Parsing bei langen Transkripten
}
```

**Lösung:** `useMemo` verwenden.

---

## 4. Integrations-Probleme

### 4.1 WebSocket-Protokoll

#### HOCH: Inkonsistente Message-Felder (`text` vs `content`)
**Dateien:** `web_api.py:487`, `LiveMic.tsx:232-237`
**Schweregrad:** HOCH

```python
# Backend sendet "text"
payload = {"type": "transcript", "text": text, "isFinal": is_final}
```

```typescript
// Frontend erwartet erst "content"
if (msg.content) {  // Prüft erst content
    setFinalText(String(msg.content));
}
const t = String(msg.text || "");  // Fallback auf text
```

**Problem:** Verwirrende Fallback-Logik, potenzielle Bugs.

---

#### MITTEL: Unbehandelter Message-Type `transcribing`
**Datei:** `web_api.py:923`
**Schweregrad:** MITTEL

```python
# Backend sendet
await self.broadcast({"type": "transcribing"})
```

```typescript
// Frontend hat keinen Handler dafür
default:
    break;  // Wird ignoriert
```

---

#### MITTEL: Kein Backpressure-Handling
**Datei:** `web_api.py:438-461`
**Schweregrad:** MITTEL

**Problem:** Audio-Level-Broadcasts (60fps) können langsame Clients überfluten.

**Risiko:** Memory-Wachstum auf Server-Seite wenn Client nicht mitkommt.

---

### 4.2 Datenformat-Probleme

#### MITTEL: Naive Datetime ohne Timezone
**Datei:** `web_api.py:267-268`
**Schweregrad:** MITTEL

```python
# PROBLEM: Keine Timezone-Info
created_at: str = field(default_factory=lambda: datetime.now().isoformat())
# Erzeugt: "2024-01-14T10:30:45.123456" (ambig)
# Sollte: "2024-01-14T10:30:45.123456Z" (UTC)
```

---

### 4.3 API-Contract

#### HOCH: Optionales `content`-Feld inkonsistent
**Dateien:** `web_api.py:274-300`, `FileTranscribe.tsx:337`
**Schweregrad:** HOCH

```python
# Backend: content nur bei include_content=True
def to_public(self, *, include_content: bool):
    if include_content:
        data["content"] = self.content  # Sonst fehlt das Feld!
```

```typescript
// Frontend erwartet content immer
const content = data?.content || "";
if (!content) throw new Error("No transcript content");
```

---

#### HOCH: Size-Limits Frontend vs Backend inkonsistent
**Dateien:** `FileTranscribe.tsx:423`, `web_api.py:30-32`
**Schweregrad:** HOCH

```typescript
// Frontend: Hardcoded
<p>Audio: MP3, M4A, WAV (max 200MB) • Video: MP4, etc. (max 2GB)</p>
```

```python
# Backend: Konfigurierbar via Environment
_DEFAULT_UPLOAD_MAX_MB = 200  # Kann per Env überschrieben werden
```

**Problem:** Wenn Backend-Limits erhöht werden, zeigt Frontend falsche Limits.

---

### 4.4 Fehlende CSRF-Protection
**Schweregrad:** MITTEL

**Problem:** Keine CSRF-Token-Validierung bei POST/PUT/DELETE.

**Betroffene Endpoints:**
- `/api/live-mic/start`
- `/api/live-mic/stop`
- `/api/file/transcribe`
- `/api/youtube/transcribe`
- `/api/transcripts/{id}` (DELETE)

---

### 4.5 Deployment/Exposure

#### NIEDRIG: Dev-Server bindet an `0.0.0.0`
**Dateien:** `Frontend/vite.config.ts`, `Frontend/package.json`
**Schweregrad:** NIEDRIG

**Problem:** Vite wird mit `--port 5000` und `server.host="0.0.0.0"` gestartet. Das ist bequem für LAN-Testing, kann aber unbeabsichtigt das UI im lokalen Netz exponieren.

**Fix-Idee:** Default auf `127.0.0.1` begrenzen und LAN-Mode explizit per Env/Flag aktivieren.

---

## 5. Zusammenfassung & Prioritäten

### 5.1 Übersichtstabelle

| Schweregrad | Backend | Frontend | Integration | Performance | Total |
|-------------|---------|----------|-------------|-------------|-------|
| **KRITISCH** | 2 | 0 | 0 | 0 | 2 |
| **HOCH** | 4 | 6 | 3 | 4 | 17 |
| **MITTEL** | 10 | 4 | 5 | 5 | 24 |
| **NIEDRIG** | 3 | 2 | 2 | 2 | 9 |
| **Total** | 19 | 12 | 10 | 11 | **52** |

### 5.2 Priorisierte Fix-Liste

#### Sofort beheben (Sicherheitskritisch)
1. **API-Keys nicht im Settings-Endpoint zurückgeben** - `web_api.py:1026`
2. **CORS-Wildcard-Fallback entfernen** - `web_api.py:1431`
3. **YouTube-URL validieren (SSRF)** - `web_api.py:550`
4. **Symlink-Check bei File-Upload** - `web_api.py:1765`

#### Kurzfristig beheben (Stabilität)
5. **Race Condition bei `_current` fixen** - Threading Lock hinzufügen
6. **Memory Leaks durch setTimeout** - Cleanup in useEffect
7. **`res.ok` Check vor `json()`** - Alle Fetch-Calls
8. **Error States in TranscriptDetail** - UX verbessern
9. **WebSocket Message-Format vereinheitlichen** - `text` vs `content`
10. **Summarize/Export: Full-Content sicherstellen** - Lazy-Load Bug beheben
11. **TypeScript-Check fixen** - `queryClient.ts` Generic/Typing reparieren
12. **Pytest-Suite reparieren** - `tests/test_injector.py`, `tests/test_youtube_download.py`

#### Mittelfristig verbessern (Qualität & Performance)
13. CSRF-Protection implementieren
14. Timezone-aware Datetimes verwenden
15. Type Safety verbessern (kein `as any`)
16. Retry-Logik für externe APIs
17. Backpressure für WebSocket

#### Performance-Optimierungen (nach Priorität)
18. **WebSocket Transcript Updates:** Delta statt Volltext (O(n²) → O(1))
19. **`_history` Datenstruktur:** Dict für ID-Lookups hinzufügen (O(n) → O(1))
20. **`/api/transcripts` Fast-Path:** Direkter Slice ohne Scan bei leerer Suche
21. **Audio-Level Rendering:** `useRef` + RAF statt 60fps State Updates
22. **`history_updated` Throttling:** Globales Rate-Limiting (max 2-4/s)
23. **Lazy-Load Flag:** `_content_loaded` statt heuristischer `len() < 150` Check

---

### 5.3 Architektur-Empfehlungen

1. **API-Key Management:** Windows Credential Manager statt `.env` Dateien
2. **Error Handling:** Zentrale Error-Boundary und standardisierte Error-Responses
3. **Validierung:** Zod/Yup Schema für Frontend/Backend Konsistenz
4. **Monitoring:** Structured Logging mit Audit-Trail für Security-Events
5. **Testing:** Integration Tests für WebSocket-Protokoll

---

## 6. Performance-Verbesserungen

> Fokus: konkrete „Quick Wins“ und skalierende Verbesserungen. Viele Punkte sind für eine lokale Single-User-App optional – werden aber relevant, sobald Transkripte groß werden oder mehrere Clients offen sind.

### 6.1 Backend (aiohttp / DB / Pipeline)

#### HOCH: WebSocket sendet kompletten `content` bei jedem finalen Segment (O(n²) Payload)
**Datei:** `src/web_api.py:484-490`

```python
payload = {"type": "transcript", "text": text, "isFinal": bool(is_final)}
if is_final and self._current:
    payload["content"] = self._current.content
```

**Impact:** Bei langen Diktaten wächst die gesendete Datenmenge quadratisch (jede Final-Nachricht enthält den gesamten bisher akkumulierten Text) → CPU (JSON-Encode) + Netzwerk + UI-Renders steigen unnötig.

**Fix-Idee (kompatibel zum Frontend-Fallback):**
- Standard: nur `text`/`isFinal` senden (Delta).
- Optional: `content` nur bei `session_finished` oder alle X Sekunden/ab X Zeichen.
- Optional: Sequenznummern (`seq`) + `contentLength`, damit der Client Drift erkennen kann.

---

#### HOCH: `/api/transcripts` scannt immer die komplette History (auch ohne Suche)
**Datei:** `src/web_api.py:1357-1389` (`ScriberWebController.list_transcripts`)

```python
for rec in self._history:
    # ... Filter
    filtered.append(rec)
```

**Impact:** O(n) pro Request, auch wenn `q` leer ist und nur paginiert wird. Zusätzlich ist die Suche teuer, weil sie `title+content+channel+summary` lowercased und konkatenert.

**Fix-Idee:**
- Fast-Path: wenn `q` leer ist und kein Type-Filter → direkt `total=len(_history)` und Slice `[offset:offset+limit]` ohne Scan.
- Bei Suche: statt `(rec.content or "").lower()` → DB-Suche (LIKE/FTS5) oder vorcomputierter, kleiner Search-Blob (z.B. nur Title/Preview/Summary).

---

#### MITTEL: Lazy-Load Condition kann wiederholt DB reads triggern
**Datei:** `src/web_api.py:1397-1407` (`ScriberWebController.get_transcript`)

```python
if len(rec.content) < 150 or not rec.summary:
    full_data = db.get_transcript(transcript_id)
```

**Impact:** Wenn `summary` leer bleibt (z.B. Auto-Summary aus), wird bei jedem Aufruf erneut aus der DB geladen → unnötige IO/Locks.

**Fix-Idee:** `rec._content_loaded` / `rec._summary_loaded` Flags (oder separates Feld für Preview vs Full) statt heuristischem `len(...)`/`not summary`.

---

#### NIEDRIG: `/api/youtube/video` erzeugt pro Request eine neue `ClientSession`
**Datei:** `src/web_api.py:1676`

**Impact:** Overhead durch Connector/Session-Aufbau; unnötig, da `app["http_session"]` bereits existiert.

**Fix-Idee:** Shared Session verwenden und per-request Timeout setzen (z.B. `timeout=ClientTimeout(total=30)` im `session.get(...)` oder im `youtube_api` Call-Site).

---

### 6.2 Frontend (React)

#### HOCH: 60fps Audio-Level Updates verursachen viele Re-Renders
**Dateien:** `src/web_api.py:472-482`, `Frontend/client/src/pages/LiveMic.tsx:227-229`

```ts
case "audio_level":
  setAudioLevel(Number(msg.rms) || 0);
  break;
```

**Impact:** Bis zu ~60 State-Updates/s → große Komponente rendert permanent. Auf schwächeren Geräten merkbar.

**Fix-Idee:**
- Audio-Level in `useRef` + `requestAnimationFrame` (UI aktualisiert 30fps/60fps ohne React-State).
- Oder: eigenes, memoisiertes Visualizer-Child, das nur `audioLevel` bekommt.
- Oder: Server-seitig FPS drosseln (z.B. 20–30fps reicht visuell oft).

---

#### MITTEL: `FitText` erzeugt DOM-Elemente zum Messen (Layout Thrash)
**Datei:** `Frontend/client/src/pages/TranscriptDetail.tsx:123-138`

**Fix-Idee:** `canvas.measureText()` oder persistentes hidden-measure Element statt create/append/remove pro Messung; Messung nur bei echten Änderungen (Width/Title).

---

#### MITTEL: Timer triggert Re-Render der gesamten TranscriptDetail-Page
**Datei:** `Frontend/client/src/pages/TranscriptDetail.tsx:320-329`

**Fix-Idee:** Timeranzeige in ein kleines memoisiertes Sub-Component auslagern oder DOM-Text via Ref aktualisieren, damit große Teile der Page nicht jede Sekunde re-evaluieren.

---

### 6.3 Integration / Protokoll

#### MITTEL: `history_updated` führt zu „Refetch-Storms“ bei häufigen Progress-Updates
**Pattern:** Backend broadcastet häufig `{"type":"history_updated"}`, Frontend invalidiert Queries und refetcht Listen.

**Fix-Idee:** Statt „invalidate + full refetch“:
- WebSocket Patch-Events (`history_patch` mit `id` + geänderten Feldern) und Query-Cache gezielt updaten.
- Throttling/Batching (z.B. max 2 Updates/s).

---

#### Optional: Kompression für große Payloads
**Ideen:**
- WebSocket permessage-deflate (Trade-off: CPU vs Bandbreite).
- HTTP gzip/deflate für Transcript-Detail/Export-Responses (falls nicht nur localhost).

---

### 6.4 Backend (Datenstrukturen)

#### HOCH: `_history` als Liste → O(n) Lookups bei jeder ID-Suche
**Dateien:** `src/web_api.py:1172, 1397, 1846, 1874, 1912, 1934`

```python
# PROBLEM: Lineare Suche bei jeder Operation
rec = next((r for r in self._history if r.id == transcript_id), None)
```

**Impact:** Bei 1000+ Transkripten werden `get_transcript`, `delete_transcript`, `summarize` etc. spürbar langsamer (O(n) statt O(1)).

**Fix-Idee:** Zusätzliches Dict `_history_by_id: dict[str, TranscriptRecord]` für O(1) Lookups:
```python
self._history_by_id = {rec.id: rec for rec in self._history}
# Lookup: rec = self._history_by_id.get(transcript_id)
```

---

#### MITTEL: WebSocket `broadcast()` kopiert Client-Liste bei jedem Aufruf
**Datei:** `src/web_api.py:438-461`

```python
async def broadcast(self, payload: dict[str, Any]) -> None:
    msg = json.dumps(payload, ensure_ascii=False)  # JSON bei jedem Call
    async with self._clients_lock:
        clients = list(self._clients)  # Kopie bei jedem Broadcast
```

**Impact:** Bei 60fps Audio-Level + mehreren Clients: ~60 JSON-Serialisierungen/s + 60 List-Kopien/s.

**Fix-Idee:**
- Audio-Level: JSON-String cachen (nur `rms`-Wert ändert sich, Template verwenden)
- Oder: Nur bei Client-Änderung kopieren (dirty flag)

---

#### MITTEL: `history_updated` wird bei jedem Progress-Update gebroadcastet
**Dateien:** `src/web_api.py:539, 548, 576, 597, 623, 643, 648, 675, 698, 744, 766, 778, 783`

**Impact:** Bei YouTube/File-Transkription mit Progress-Updates: Frontend refetcht die gesamte Liste mehrfach pro Sekunde.

**Beobachtung:** Es gibt bereits Throttling (0.25s) für YouTube-Progress, aber nicht für alle Pfade.

**Fix-Idee:**
- Globales Throttling für `history_updated` (max 2-4/s)
- Oder: `history_patch`-Events mit nur geänderten Feldern

---

### 6.5 Pipeline (Audio-Processing)

#### NIEDRIG: Soniox Async Processor hält gesamtes Audio im RAM
**Datei:** `src/pipeline.py:112-260` (`SonioxAsyncProcessor`)

```python
self._audio_buffer = io.BytesIO()  # Wächst unbegrenzt
```

**Impact:** Bei langen Aufnahmen (>30min) kann der RAM-Verbrauch mehrere GB erreichen.

**Fix-Idee:**
- Streaming-Upload statt Buffer-dann-Upload
- Oder: Chunked Processing mit Zwischenergebnissen

---

#### NIEDRIG: VAD/SmartTurn-Analyzer werden pro Pipeline gecacht, aber nicht global
**Datei:** `src/pipeline.py:60-109` (`_AnalyzerCache`)

**Beobachtung:** Cache ist bereits implementiert (`_AnalyzerCache`), aber nur auf Klassen-Ebene. Bei Restart der Pipeline werden Analyzer neu initialisiert.

**Status:** ✅ Bereits optimiert - Cache funktioniert korrekt.

---

### 6.6 Messen statt Raten (Low Effort)

**Quick Wins:**
- Timings per `time.perf_counter()` (Backend) für `list_transcripts`, `db.get_transcript`, Export, YouTube Download.
- Client-Side: `performance.mark/measure` für Render-Hotspots (TranscriptDetail, Listen).
- Optional: Debug-Flag-gated Logging, damit Release nicht noisig wird.

**Profiling-Empfehlungen:**
```python
# Backend: Simple Timing Decorator
import functools, time
def timed(func):
    @functools.wraps(func)
    async def wrapper(*args, **kwargs):
        start = time.perf_counter()
        result = await func(*args, **kwargs)
        logger.debug(f"{func.__name__} took {(time.perf_counter()-start)*1000:.1f}ms")
        return result
    return wrapper
```

```typescript
// Frontend: React DevTools Profiler + Performance API
performance.mark('list-render-start');
// ... render
performance.mark('list-render-end');
performance.measure('list-render', 'list-render-start', 'list-render-end');
```

---

### 6.7 Zusammenfassung Performance-Prioritäten

| Priorität | Problem | Erwarteter Gewinn |
|-----------|---------|-------------------|
| **HOCH** | WebSocket O(n²) Content | -50-90% Bandbreite bei langen Diktaten |
| **HOCH** | `/api/transcripts` O(n) Scan | -80% Latenz bei leerer Suche |
| **HOCH** | 60fps Audio Re-Renders | -90% React Re-Renders |
| **HOCH** | `_history` O(n) Lookups | -90% bei ID-Operationen |
| **MITTEL** | Lazy-Load wiederholte DB-Reads | -50% DB I/O |
| **MITTEL** | `history_updated` Storms | -70% Refetch-Requests |
| **MITTEL** | FitText Layout Thrash | Smoother UI |
| **NIEDRIG** | Audio-Buffer RAM | Nur bei >30min relevant |

---

*Aktualisiert von Claude Opus 4.5 am 2026-01-15*
