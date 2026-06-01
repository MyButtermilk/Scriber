# Performance Optimization Proposals

## Overview

This document outlines potential performance improvements for the Scriber application across backend, frontend, and architecture layers.

---

## Priority 1: High-Impact, Low-Effort

### 1.1 Database Connection Pooling ✅ IMPLEMENTED

**Current State:**
- `database.py` creates a new SQLite connection for every database operation (`_get_connection()`)
- Each query opens and closes a connection

**Problem:**
- Connection overhead adds latency to every transcript load/save operation
- Not thread-safe under concurrent access

**Implemented Solution:**
```python
# src/database.py - Thread-local connection pooling
import threading
import atexit

_thread_local = threading.local()
_all_connections: list[sqlite3.Connection] = []

def _get_connection() -> sqlite3.Connection:
    if not hasattr(_thread_local, 'conn') or _thread_local.conn is None:
        conn = sqlite3.connect(_DB_PATH, check_same_thread=False, timeout=30.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")  # Better concurrency
        conn.execute("PRAGMA synchronous=NORMAL")  # Faster writes
        _thread_local.conn = conn
    return _thread_local.conn

atexit.register(_close_all_connections)  # Cleanup on exit
```

**Impact:** ~10-50ms reduction per database operation
**Status:** ✅ Completed (2026-01-01)

---

### 1.2 Frontend Bundle Optimization ✅ IMPLEMENTED (Code Splitting + Vendor Chunks)

**Current State:**
- React 19 with Vite 7, full bundle loaded on initial page load
- Large dependencies: framer-motion, recharts, lucide-react (all icons), radix-ui

**Implemented Solution - Code Splitting:**
```tsx
// Frontend/client/src/App.tsx
import { lazy, Suspense } from "react";
import LiveMic from "@/pages/LiveMic";

// Non-default pages are separate chunks, loaded on demand.
// LiveMic stays eager for fastest first paint.
const Youtube = lazy(() => import("@/pages/Youtube"));
const FileTranscribe = lazy(() => import("@/pages/FileTranscribe"));
const Settings = lazy(() => import("@/pages/Settings"));
const TranscriptDetail = lazy(() => import("@/pages/TranscriptDetail"));

function TabRoutes() {
  return (
    <Suspense fallback={<PageLoader />}>
      <Switch>
        <Route path="/" component={LiveMic} />
        {/* ... */}
      </Switch>
    </Suspense>
  );
}
```

**Remaining Optimizations (Optional):**
- Tree-shake Lucide Icons using individual imports
- Lazy load Framer Motion for animated pages only

**Impact:** 30-50% reduction in initial bundle size
**Status:** ✅ Route code splitting completed (2026-01-01); ✅ manual vendor chunks completed (2026-06-01). Latest `npm run build` succeeds without the previous 500 kB initial chunk warning.

---

### 1.3 WebSocket Singleton Hook ✅ IMPLEMENTED

**Current State:**
- ~~Jede Seite erstellt eigene WebSocket-Verbindung~~ → Single shared connection via Context

**Implemented Solution:**
```typescript
// Frontend/client/src/contexts/WebSocketContext.tsx
export function WebSocketProvider({ children, path = "/ws", autoReconnect = true, ... }) {
  const wsRef = useRef<WebSocket | null>(null);
  const subscribersRef = useRef<Set<MessageHandler>>(new Set());

  // Single connection, broadcasts to all subscribers
  wsRef.current.onmessage = (event) => {
    const data = JSON.parse(event.data);
    subscribersRef.current.forEach(handler => handler(data));
  };
}

export function useSharedWebSocket(onMessage: MessageHandler) {
  const { subscribe } = useWebSocketContext();
  useEffect(() => subscribe(onMessage), [onMessage, subscribe]);
}
```

**Updated Files:**
- NEW: `Frontend/client/src/contexts/WebSocketContext.tsx` - WebSocket provider
- `Frontend/client/src/App.tsx` - Added WebSocketProvider wrapper
- `Frontend/client/src/pages/LiveMic.tsx` - Uses `useSharedWebSocket`
- `Frontend/client/src/pages/Youtube.tsx` - Uses `useSharedWebSocket`
- `Frontend/client/src/pages/FileTranscribe.tsx` - Uses `useSharedWebSocket`
- `Frontend/client/src/pages/TranscriptDetail.tsx` - Uses `useSharedWebSocket`
- `Frontend/client/src/components/RecordingPopup.tsx` - Uses `useSharedWebSocket`

**Vorteile:**
- 1 statt 5 TCP-Verbindungen = weniger Server-Load
- Schnellere Navigation (keine neue Verbindung beim Seitenwechsel)
- Konsistenter State über alle Komponenten
- Weniger Memory (ein WebSocket-Buffer statt fünf)

**Impact:** ~200-400ms Netzwerk-Latenz gespart, weniger Server-Load
**Status:** ✅ Completed (2026-01-13)

---

### 1.4 Component Memoization ✅ IMPLEMENTED

**Current State:**
- `LiveMic.tsx:294-374` rendert Transcript-Cards inline in `.map()`
- `Youtube.tsx:403-523` rendert Video-Cards inline
- `FileTranscribe.tsx:266-364` rendert File-Cards inline

**Problem:**
- Bei *jedem* State-Change im Parent werden *alle* Cards neu gerendert
- Bei 50 Transcripts = 50 unnötige Re-Renders pro State-Änderung
- Besonders bei `audioLevels` Updates (30fps) problematisch

**Proposed Solution:**
```tsx
// Vorher: Inline in der map() - SCHLECHT
{transcripts.map((item) => (
  <motion.div key={item.id}>
    <Card>...</Card>
  </motion.div>
))}

// Nachher: Separate memoized Komponente - GUT
const TranscriptCard = memo(function TranscriptCard({
  item,
  onDelete,
  onNavigate
}: TranscriptCardProps) {
  return (
    <motion.div initial={{...}} animate={{...}}>
      <Card className="...">
        {/* Card-Inhalt */}
      </Card>
    </motion.div>
  );
});

// WICHTIG: Callbacks müssen stabil sein!
const handleDelete = useCallback((id: string) => {
  deleteMutation.mutate(id);
}, [deleteMutation]);

const handleNavigate = useCallback((id: string) => {
  setLocation(`/transcript/${id}`);
}, [setLocation]);

// Verwendung
{transcripts.map((item) => (
  <TranscriptCard
    key={item.id}
    item={item}
    onDelete={handleDelete}
    onNavigate={handleNavigate}
  />
))}
```

**Vorteile:**
- Card rendert nur bei eigener Prop-Änderung neu
- Flüssigere UI, besonders bei vielen Items
- Bessere Code-Struktur (kleinere, wiederverwendbare Komponenten)
- Skaliert besser bei 100+ Transcripts

**Nachteile:**
- Overhead bei wenigen Items (memo-Vergleich kostet auch)
- Callbacks MÜSSEN mit `useCallback` stabilisiert werden (sonst wirkungslos!)
- Mehr Boilerplate (separate Komponente + Props-Interface)
- Bei Object-Props braucht man ggf. custom `areEqual` Funktion

**Betroffene Dateien:**
- `Frontend/client/src/pages/LiveMic.tsx` (Zeilen 294-374)
- `Frontend/client/src/pages/Youtube.tsx` (Zeilen 403-523)
- `Frontend/client/src/pages/FileTranscribe.tsx` (Zeilen 266-364)

**Impact:** ~100-200ms UI Response-Verbesserung
**Effort:** Low (2-3 Stunden)
**Risk:** Gering (lokale Änderung, leicht rückgängig zu machen)
**Status:** ✅ Completed (2026-01-01) - Implemented memoized components: `TranscriptCard`, `YoutubeVideoCard`, `FileCard`

---

### 1.5 Vite Build Optimization ✅ IMPLEMENTED

**Previous State:**
- `vite.config.ts` had minimal build configuration:
```typescript
build: {
  outDir: path.resolve(import.meta.dirname, "dist/public"),
  emptyOutDir: true,
}
```

**Problem:**
- No `rollupOptions.output` for chunk control.
- Large vendor chunks were grouped together.

**Implemented Solution:**
```typescript
// Frontend/vite.config.ts
build: {
  outDir: path.resolve(import.meta.dirname, "dist/public"),
  emptyOutDir: true,
  rollupOptions: {
    output: {
      manualChunks(id) {
        const normalizedId = id.replace(/\\/g, "/");
        if (!normalizedId.includes("/node_modules/")) return undefined;
        if (normalizedId.includes("/node_modules/react/")) return "vendor-react";
        if (normalizedId.includes("/node_modules/@tanstack/")) return "vendor-query";
        if (normalizedId.includes("/node_modules/framer-motion/")) return "vendor-motion";
        if (normalizedId.includes("/node_modules/recharts/")) return "vendor-charts";
        return "vendor";
      }
    }
  }
}
```

**Vorteile:**
- Bessere Cache-Nutzung (Vendor-Chunks ändern sich selten)
- Paralleles Laden von unabhängigen Chunks
- Kleinere initiale Bundle-Größe

**Impact:** ~15-25% Bundle-Reduktion, besseres Caching
**Effort:** Low (1 Stunde)
**Risk:** Gering
**Status:** ✅ Completed (2026-06-01). Build output now separates React, TanStack Query, motion libraries, and the remaining vendor chunk. Radix stays in the remaining vendor chunk to avoid circular manual-chunk warnings with the current dependency graph.

---

### 1.6 WebSocket Message Throttling ✅ IMPLEMENTED

**Current State:**
- `ScriberWebController._on_audio_level()` throttles audio-level broadcasts to ~30fps.
- `MicrophoneInput._audio_callback()` also throttles visualizer/input-warning RMS work to ~30fps before the WebSocket path.
- `history_updated` broadcasts are globally throttled/coalesced to avoid refetch storms.
- `broadcast()` validates optional contracts first, then refreshes the client snapshot and returns before `json.dumps()` when no WebSocket clients are connected.
- `_on_audio_level()` skips UI broadcast scheduling when no WebSocket clients are connected and the native overlay is not enabled.

**Problem:**
- High message frequency used to do unnecessary backend serialization/task work when no UI was connected.
- Frontend visualizer rendering is already isolated from broad page re-renders.

**Implemented throttle + no-client fast path:**
```python
class ScriberWebController:
    async def broadcast(self, payload: dict[str, Any]) -> None:
        clients = self._clients_snapshot
        if not clients:
            return
        msg = json.dumps(payload, ensure_ascii=False)

    def _on_audio_level(self, rms: float, *, session_id: str | None = None) -> None:
        has_ws_clients = self._has_ws_clients()
        if not has_ws_clients and not self._overlay_audio_enabled:
            return
```

**Impact:** Audio-level and history-update traffic are reduced, and idle/no-client sessions avoid repeated JSON serialization and broadcast task scheduling.
**Status:** ✅ Completed (2026-06-01). Covered by `tests/test_web_api_lifecycle.py`.

---

## Priority 2: Medium-Impact Optimizations

### 2.1 Database Index on created_at ✅ IMPLEMENTED

**Current State:**
- ~~`database.py` erstellt Tabelle ohne expliziten Index~~ → Index hinzugefügt

**Implemented Solution:**
```python
# src/database.py - In init_database()
def init_database() -> None:
    with _get_connection() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS transcripts (...)
        """)
        # PERFORMANCE: Index on created_at for faster ORDER BY queries
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_transcripts_created_at
            ON transcripts(created_at DESC)
        """)
        conn.commit()
```

**Impact:** ~50-100ms Verbesserung bei 1000+ Transcripts
**Status:** ✅ Completed (2026-01-13)

---

### 2.2 Transcript List Pagination ✅ BACKEND IMPLEMENTED

**Current State:**
- ~~`list_transcripts()` returns all transcripts~~ → Backend now supports pagination

**Implemented Solution - Backend Pagination:**
```python
# src/web_api.py - list_transcripts()
def list_transcripts(self, *, include_content: bool = False, query: str = "",
                     transcript_type: str = "", offset: int = 0, limit: int = 50) -> dict[str, Any]:
    # Returns paginated response
    return {
        "items": transcripts[offset:offset + limit],
        "total": len(transcripts),
        "offset": offset,
        "limit": limit,
        "hasMore": offset + limit < len(transcripts),
    }
```

**API Endpoint:**
- `GET /api/transcripts?offset=0&limit=50` - Returns paginated results
- Response includes `items`, `total`, `offset`, `limit`, `hasMore`

**Implemented Frontend Virtual Scrolling:**
The Live Mic, File, and YouTube history views now use `@tanstack/react-virtual`
with React Query infinite pagination:
```typescript
const transcriptsQuery = useTranscriptHistoryQuery({ type: "mic", q: debouncedSearch });

<VirtualTranscriptHistory
  items={transcriptsQuery.items}
  hasMore={transcriptsQuery.hasNextPage}
  onLoadMore={() => transcriptsQuery.fetchNextPage()}
/>;
```

**Impact:** Handle 10,000+ transcripts without performance degradation
**Status:** ✅ Backend completed (2026-01-13), ✅ Frontend infinite query + virtualization completed (2026-06-01)

---

### 2.2 STT Pipeline Warm-up Cache ✅ IMPLEMENTED

**Current State:**
- Each recording session creates a new `ScriberPipeline`
- STT service, VAD, and analyzers are initialized fresh

**Problem:**
- ~300-800ms overhead per recording start
- Network latency for STT service handshake

**Implemented Solution:**
```python
# src/pipeline.py - _AnalyzerCache class
class _AnalyzerCache:
    """Thread-safe cache for expensive analyzers (VAD, SmartTurn)."""
    _lock = threading.Lock()
    _vad_analyzer = None
    _smart_turn_analyzer = None
    
    @classmethod
    def get_vad_analyzer(cls):
        with cls._lock:
            if cls._vad_analyzer is None:
                cls._vad_analyzer = SileroVADAnalyzer()
            return cls._vad_analyzer
```

**Impact:** Reduce recording start latency by 200-500ms
**Status:** ✅ Completed (2026-01-01)

---

### 2.3 Audio Frame Processing Optimization ✅ IMPLEMENTED

**Current State:**
- `MicrophoneInput._audio_callback()` processes audio synchronously
- Raw audio is still queued on every callback
- UI-only RMS and input-warning work is capped to the UI frame rate
- Multi-channel capture rescans the strongest channel periodically instead of recomputing full channel energy every callback

**Implemented Solution:**
```python
# src/microphone.py - _audio_callback method
def _audio_callback(self, indata, frames, time, status):
    output_data = select_or_reuse_capture_channel(indata)
    audio_bytes = output_data.tobytes()
    self._loop.call_soon_threadsafe(self._queue.put_nowait, audio_bytes)

    # Throttled UI/RMS calculation (~30fps); audio frames are not dropped.
    if self.on_audio_level and enough_time_elapsed():
        samples = np.asarray(output_data).astype(np.int16, copy=False).ravel()
        rms = np.sqrt(np.mean(samples.astype(np.float32) ** 2)) / 32768.0
        self.on_audio_level(float(rms))
```

**Impact:** Lower callback CPU during recording without sacrificing STT audio throughput.
**Status:** ✅ Initial optimization completed (2026-01-01); ✅ channel-rescan + 30fps UI/RMS throttle updated (2026-06-01).

---

### 2.4 Device Monitor and Mic Device Resolution ✅ IMPLEMENTED

**Current State:**
- `DeviceMonitor` uses native Windows endpoint notifications when available, with a slower polling fallback.
- The frontend also sends a debounced `/api/microphones/refresh` hint from browser/WebView `devicechange` events while the UI is open.
- PortAudio cache refresh is deferred while an input stream is active, then executed once after the stream becomes idle.
- Microphone enumeration and stream open/close share the same guard lock.
- `_resolve_mic_device()` caches name/favorite-to-index resolution for repeated recording starts.

**Implemented Solution:**
```python
# src/device_monitor.py
default_poll_seconds = 60.0 if self._supports_native_events else 10.0

if _ACTIVE_STREAMS > 0:
    return False, True  # defer PortAudio reinitialization until idle

# src/pipeline.py
SCRIBER_MIC_DEVICE_CACHE_TTL_SEC = 10.0  # default
```

**Impact:** Fewer PortAudio refreshes during active recordings, less log noise, lower start-path overhead on repeated recordings, and faster hotplug UI refreshes without shortening fallback polling intervals.
**Status:** ✅ Completed (2026-06-01)

---

## Priority 3: Architecture Improvements

### 3.1 Async Database Operations

**Current State:**
- SQLite operations are synchronous
- Can block the async event loop

**Proposed Solution:**
- Use `aiosqlite` for non-blocking database access:
  ```python
  import aiosqlite
  
  async def save_transcript(record):
      async with aiosqlite.connect(_DB_PATH) as db:
          await db.execute("INSERT ...", params)
          await db.commit()
  ```

**Impact:** Improved responsiveness during heavy DB operations
**Effort:** High (8-12 hours, requires refactoring all DB calls)

---

### 3.2 Service Worker for Frontend Caching

**Current State:**
- No offline support
- All assets re-fetched on reload

**Proposed Solution:**
- Add Vite PWA plugin for service worker generation:
  ```typescript
  // vite.config.ts
  import { VitePWA } from 'vite-plugin-pwa';
  
  export default defineConfig({
    plugins: [
      VitePWA({
        registerType: 'autoUpdate',
        workbox: {
          globPatterns: ['**/*.{js,css,html,ico,png,svg}']
        }
      })
    ]
  });
  ```

**Impact:** Instant page loads after first visit
**Effort:** Medium (4-6 hours)

---

### 3.3 Memory-Efficient Transcript Storage ✅ IMPLEMENTED

**Current State:**
- ~~Full transcript content stored in memory~~ → Lazy loading implemented

**Implemented Solution:**

1. **Metadata-only loading for list views:**
```python
# src/database.py - load_transcript_metadata()
def load_transcript_metadata() -> List[dict]:
    """Load transcript metadata without content for fast list views.

    PERFORMANCE: Excludes content and summary fields which can be very large.
    Reduces memory usage by 80-90% for large transcript lists.
    """
    cursor = conn.execute("""
        SELECT id, title, date, duration, status, type, language, step,
               source_url, channel, thumbnail_url, created_at, updated_at,
               substr(content, 1, 100) as preview_text
        FROM transcripts ORDER BY created_at DESC
    """)
```

2. **On-demand content loading:**
```python
# src/web_api.py - get_transcript()
def get_transcript(self, transcript_id: str) -> Optional[dict[str, Any]]:
    rec = self._transcripts.get(transcript_id)
    if rec and len(rec.content) < 150:  # Lazy load check
        full_data = database.get_transcript(transcript_id)
        if full_data:
            rec.content = full_data.get("content", "")
            rec.summary = full_data.get("summary", "")
    return rec.to_public(include_content=True)
```

3. **Buffered live transcript appends:**
```python
# src/web_api.py - TranscriptRecord.append_final_text()
if not self.content and not self._pending_content_segments:
    self.content = cleaned
else:
    self._pending_content_segments.append(cleaned)
```

`content_text()` materializes pending segments only when full content is explicitly
requested or the session finishes. `scripts/check_transcript_buffer_growth.py`
guards the Phase 8 long-session shape by simulating one final segment per second
for 30 minutes and failing if metadata reads materialize the growing transcript
string during append.

**Updated Files:**
- `src/database.py` - Added `load_transcript_metadata()` function
- `src/web_api.py` - Uses metadata loading for lists, lazy loads content on demand
- `scripts/check_transcript_buffer_growth.py` - Synthetic 30-minute transcript string-growth guard
- `tests/perf/test_transcript_buffer_growth_script.py` - Guard-script coverage

**Impact:** 80-90% memory reduction for transcript lists (10MB → 1MB for 1000 transcripts)
**Status:** ✅ Completed (2026-01-13); long live transcript append guard added 2026-06-02

---

## Implementation Roadmap

### Completed ✅
- [x] Database connection pooling (1.1) ✅ (2026-01-01)
- [x] Code splitting (1.2) ✅ (2026-01-01)
- [x] **WebSocket Singleton Hook (1.3) ✅ (2026-01-13)** - ~200-400ms Impact
- [x] Component Memoization (1.4) ✅ (2026-01-01)
- [x] **Database Index on created_at (2.1) ✅ (2026-01-13)** - 50-100ms Impact
- [x] **Transcript Pagination API (2.2) ✅ (2026-01-13)** - Backend complete
- [x] STT/Analyzer caching (2.2) ✅ (2026-01-01)
- [x] Audio frame optimization (2.3) ✅ (2026-01-01)
- [x] **DeviceMonitor deferred refresh + safer PortAudio locking ✅ (2026-06-01)**
- [x] **Microphone device resolution cache ✅ (2026-06-01)**
- [x] **Audio callback channel-rescan/RMS throttle update ✅ (2026-06-01)**
- [x] **Per-session keep_alive cleanup forced closed ✅ (2026-06-01)** - prevents orphaned PortAudio resources until a true app-level always-on mic manager exists
- [x] **Lazy transcript content loading (3.3) ✅ (2026-01-13)** - 80-90% Memory reduction
- [x] **Long live transcript append buffering guard ✅ (2026-06-02)** - synthetic 30-minute segment-growth check
- [x] Lazy STT imports (2026-01-01) ✅
- [x] Background overlay prewarming (2026-01-01) ✅
- [x] Background ML model prewarming (2026-01-01) ✅
- [x] Background transcript loading (2026-01-01) ✅
- [x] STT service pre-import / Hotkey response optimization (4.4) ✅ (2026-01-01)

### Pending (High Priority)
- [ ] Vite Build Optimization (1.5) - ~15-25% Bundle, Low Effort
- [ ] Generic WebSocket message batching (1.6 follow-up) - evaluate only if measured broadcast cost becomes meaningful with connected clients

### Pending (Medium Priority)
- [x] Frontend Virtual Scrolling (2.2) ✅ (2026-06-01) - Uses pagination API with infinite scroll and virtualized history rows
- [ ] True app-level microphone prewarming manager - current `MIC_ALWAYS_ON` flag does not keep a reusable per-app stream alive
- [ ] Background upload preprocessing for large files - upload writes/exports are off the event loop and `measure_upload_export_baseline.py` now probes `/api/health` and `/api/state` responsiveness under synthetic load, but heavy compression/transcription preprocessing is still request-scoped instead of fully job-detached

### Future
- [ ] Async database (3.1)
- [ ] PWA/Service Worker (3.2)

---

## Metrics to Track

Status 2026-06-02: `scripts/measure_hybrid_baseline.ps1` creates a JSON baseline artifact for the hybrid Tauri/Python runtime. It measures startup/backend readiness, reads available hot-path metric segments, can opt into live recording samples with `scripts/measure_recording_hot_path_baseline.py`, embeds `scripts/measure_upload_export_baseline.py` results for synthetic upload/export load and `/api/health`/`/api/state` responsiveness under that load, embeds `scripts/measure_ws_broadcast_baseline.py` results for WebSocket throughput and JSON serialization, and embeds `scripts/measure_history_scroll_baseline.py` results for synthetic browser history scrolling against paginated transcript history. `scripts/smoke_frontend_browser.py` adds a separate real-browser frontend route smoke with a synthetic backend and console/page-error checks for Live Mic, YouTube, File, Settings, and Transcript Detail. The Phase 0 gate intentionally stays incomplete until a real spoken/injected sample captures stop-to-text-injection timing; async injection uses `stop_requested_to_first_paste_ms`, while realtime text already injected before stop uses `first_paste_to_stop_requested_ms` and records `0 ms` stop-to-text wait.

| Metric | Before | After | Target | Status |
|--------|--------|-------|--------|--------|
| Recording start latency | ~800ms | ~300ms + cached repeated device resolution | <300ms | ✅ Improved |
| Initial page load (LCP) | ~2.5s | ~1.8s | <1.5s | 🔄 Improved |
| Memory usage (100 transcripts) | ~15MB | ~2MB | <5MB | ✅ Achieved |
| WebSocket connections | 5 | 1 | 1 | ✅ Achieved |
| WebSocket messages/sec (recording) | ~60/s | ~30/s for audio_level | <30/s | ✅ Achieved for audio_level |
| Bundle size (gzipped) | ~800KB | ~650KB | <400KB | 🔄 Improved |
| Database query time (1000+ items) | ~150ms | ~50ms | <100ms | ✅ Achieved |

---

## Notes

- Always benchmark before and after changes
- Use the browser DevTools Performance panel for frontend profiling
- Use `loguru` timing decorators for backend profiling:
  ```python
  from loguru import logger
  import time
  
  def timed(func):
      def wrapper(*args, **kwargs):
          start = time.perf_counter()
          result = func(*args, **kwargs)
          logger.debug(f"{func.__name__} took {time.perf_counter() - start:.3f}s")
          return result
      return wrapper
  ```
