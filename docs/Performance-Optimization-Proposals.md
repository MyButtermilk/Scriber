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

### 1.2 Frontend Bundle Optimization ✅ IMPLEMENTED (Code Splitting)

**Current State:**
- React 19 with Vite 7, full bundle loaded on initial page load
- Large dependencies: framer-motion, recharts, lucide-react (all icons), radix-ui

**Implemented Solution - Code Splitting:**
```tsx
// Frontend/client/src/App.tsx
import { lazy, Suspense } from "react";

// Each page is now a separate chunk, loaded on demand
const LiveMic = lazy(() => import("@/pages/LiveMic"));
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
**Status:** ✅ Code Splitting Completed (2026-01-01)

---

### 1.3 WebSocket Singleton Hook

**Current State:**
- Jede Seite erstellt eigene WebSocket-Verbindung:
  - `LiveMic.tsx` → `new WebSocket("/ws")`
  - `Youtube.tsx` → `new WebSocket("/ws")`
  - `FileTranscribe.tsx` → `new WebSocket("/ws")`
  - `TranscriptDetail.tsx` → `new WebSocket("/ws")`
  - `RecordingPopup.tsx` → `new WebSocket("/ws")`

**Problem:**
- 5 parallele TCP-Verbindungen zum gleichen Endpoint
- Jede Verbindung = TCP-Handshake-Overhead
- Inkonsistenter State möglich zwischen Komponenten

**Proposed Solution:**
```typescript
// Frontend/client/src/hooks/useWebSocket.ts
let wsInstance: WebSocket | null = null;
const listeners = new Map<string, Set<(data: any) => void>>();

export function useWebSocket(
  messageTypes: string[],
  onMessage: (data: any) => void
) {
  const stableCallback = useCallback(onMessage, []);

  useEffect(() => {
    // Nur EINE Verbindung für die ganze App
    if (!wsInstance || wsInstance.readyState === WebSocket.CLOSED) {
      wsInstance = new WebSocket(wsUrl("/ws"));
      wsInstance.onmessage = (e) => {
        const data = JSON.parse(e.data);
        listeners.get(data.type)?.forEach(cb => cb(data));
      };
    }

    // Komponente registriert sich für bestimmte Message-Typen
    messageTypes.forEach(type => {
      if (!listeners.has(type)) listeners.set(type, new Set());
      listeners.get(type)!.add(stableCallback);
    });

    return () => {
      messageTypes.forEach(type => listeners.get(type)?.delete(stableCallback));
    };
  }, [messageTypes, stableCallback]);
}
```

**Vorteile:**
- 1 statt 5 TCP-Verbindungen = weniger Server-Load
- Schnellere Navigation (keine neue Verbindung beim Seitenwechsel)
- Konsistenter State über alle Komponenten
- Weniger Memory (ein WebSocket-Buffer statt fünf)

**Nachteile:**
- Komplexere Message-Routing-Logik
- Shared State Risiko (Bug in einem Listener kann andere beeinflussen)
- Singleton-Pattern erschwert Unit-Tests
- Migration erfordert Anpassungen in 5 Dateien

**Impact:** ~200-400ms Netzwerk-Latenz gespart, weniger Server-Load
**Effort:** Medium (4-6 Stunden)
**Risk:** Mittel (Bug könnte gesamte Real-time-Kommunikation brechen)

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

### 1.5 Vite Build Optimization

**Current State:**
- `vite.config.ts:37-40` hat minimale Build-Konfiguration:
```typescript
build: {
  outDir: path.resolve(import.meta.dirname, "dist/public"),
  emptyOutDir: true,
}
```

**Problem:**
- Keine `rollupOptions.output` für Chunk-Kontrolle
- Keine explizite `minify` Spezifikation
- Große Vendor-Chunks (React, Radix, Framer Motion zusammen)

**Proposed Solution:**
```typescript
// Frontend/vite.config.ts
build: {
  outDir: path.resolve(import.meta.dirname, "dist/public"),
  emptyOutDir: true,
  minify: 'terser',
  terserOptions: {
    compress: {
      drop_console: true,  // Entferne console.log in Production
    },
  },
  rollupOptions: {
    output: {
      manualChunks: {
        'react-vendor': ['react', 'react-dom'],
        'radix-ui': [
          '@radix-ui/react-dialog',
          '@radix-ui/react-popover',
          '@radix-ui/react-select',
          '@radix-ui/react-tabs',
        ],
        'animation': ['framer-motion'],
        'query': ['@tanstack/react-query'],
      }
    }
  },
  reportCompressedSize: true,
}
```

**Vorteile:**
- Bessere Cache-Nutzung (Vendor-Chunks ändern sich selten)
- Paralleles Laden von unabhängigen Chunks
- Kleinere initiale Bundle-Größe
- Console.logs in Production entfernt

**Impact:** ~15-25% Bundle-Reduktion, besseres Caching
**Effort:** Low (1 Stunde)
**Risk:** Gering

---

### 1.6 WebSocket Message Batching

**Current State:**
- `ScriberWebController.broadcast()` sends individual messages for each event
- Audio level updates fire continuously during recording

**Problem:**
- High message frequency can overwhelm the frontend
- Each `audio_level` update triggers a React re-render

**Proposed Solution:**
```python
# Throttle audio level broadcasts to max 30fps
class ScriberWebController:
    def __init__(self, ...):
        self._last_audio_broadcast = 0
        self._audio_broadcast_interval = 1/30  # 30fps

    def _on_audio_level(self, rms: float):
        now = time.monotonic()
        if now - self._last_audio_broadcast < self._audio_broadcast_interval:
            return  # Skip this update
        self._last_audio_broadcast = now
        # ... existing broadcast logic
```

**Impact:** Reduce WebSocket traffic by 50-70%, smoother UI
**Effort:** Low (2 hours)

---

## Priority 2: Medium-Impact Optimizations

### 2.1 Database Index on created_at

**Current State:**
- `database.py:63-85` erstellt Tabelle ohne expliziten Index:
```sql
CREATE TABLE IF NOT EXISTS transcripts (
    id TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,  -- Kein Index!
    updated_at TEXT NOT NULL,
    ...
)
```

**Problem:**
- `load_all_transcripts()` sortiert nach `created_at DESC`
- Ohne Index = Full Table Scan bei jeder Abfrage
- Bei 1000+ Transcripts spürbar langsamer

**Proposed Solution:**
```python
# src/database.py - In init_database()
def init_database() -> None:
    with _get_connection() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS transcripts (...)
        """)
        # NEU: Index für schnellere Sortierung
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_transcripts_created_at
            ON transcripts(created_at DESC)
        """)
        conn.commit()
```

**Impact:** ~50-100ms Verbesserung bei 1000+ Transcripts
**Effort:** Very Low (15 Minuten)
**Risk:** Sehr gering (Index kann ohne Datenverlust hinzugefügt werden)

---

### 2.2 Transcript List Virtualization

**Current State:**
- `list_transcripts()` returns all transcripts
- Frontend renders entire list

**Problem:**
- With 100+ transcripts, DOM becomes heavy
- Initial load time increases linearly

**Proposed Solution:**
1. **Backend Pagination:**
   ```python
   def list_transcripts(self, *, offset: int = 0, limit: int = 20):
       # Add LIMIT and OFFSET to SQL query
   ```

2. **Frontend Virtual Scrolling:**
   Use `@tanstack/react-virtual` or native Intersection Observer:
   ```typescript
   const TranscriptList = () => {
     const { data, fetchNextPage } = useInfiniteQuery({
       queryKey: ['transcripts'],
       queryFn: ({ pageParam = 0 }) => 
         fetch(`/api/transcripts?offset=${pageParam}&limit=20`)
     });
   };
   ```

**Impact:** Handle 10,000+ transcripts without performance degradation
**Effort:** Medium (6-8 hours)

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
- RMS calculation done per frame

**Implemented Solution:**
```python
# src/microphone.py - _audio_callback method
def _audio_callback(self, indata, frames, time, status):
    audio_bytes = indata.tobytes()
    self._loop.call_soon_threadsafe(self._queue.put_nowait, audio_bytes)
    
    # Throttled RMS calculation (every 2nd callback = ~30fps)
    self._rms_callback_count += 1
    if self.on_audio_level and (self._rms_callback_count & 1) == 0:
        samples = indata.view(np.int16).ravel()
        # Use float32 for faster computation
        rms = np.sqrt(np.mean(samples.astype(np.float32) ** 2)) / 32768.0
        self.on_audio_level(float(rms))
```

**Impact:** ~10-20% CPU reduction during recording, 50% fewer callbacks
**Status:** ✅ Completed (2026-01-01)

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

### 3.3 Memory-Efficient Transcript Storage

**Current State:**
- Full transcript content stored in memory (`TranscriptRecord.content`)
- All transcripts loaded from DB on startup

**Problem:**
- 1000 transcripts × 10KB avg = 10MB+ memory usage

**Proposed Solution:**
```python
# Lazy-load content only when needed
class TranscriptRecord:
    _content: Optional[str] = None
    
    @property
    def content(self) -> str:
        if self._content is None:
            self._content = database.get_transcript_content(self.id)
        return self._content
```

**Impact:** 90% reduction in memory for transcript list
**Effort:** Medium (4-6 hours)

---

## Implementation Roadmap

### Completed ✅
- [x] Database connection pooling (1.1) ✅
- [x] Code splitting (1.2) ✅
- [x] STT/Analyzer caching (2.2) ✅
- [x] Audio frame optimization (2.3) ✅
- [x] Lazy STT imports (2026-01-01) ✅
- [x] Background overlay prewarming (2026-01-01) ✅
- [x] Background ML model prewarming (2026-01-01) ✅
- [x] Background transcript loading (2026-01-01) ✅
- [x] STT service pre-import / Hotkey response optimization (4.4) (2026-01-01) ✅

### Pending (High Priority)
- [ ] WebSocket Singleton Hook (1.3) - ~200-400ms Impact, Medium Effort
- [x] Component Memoization (1.4) - ~100-200ms Impact ✅ Completed (2026-01-01)
- [ ] Vite Build Optimization (1.5) - ~15-25% Bundle, Low Effort
- [ ] WebSocket Message Batching (1.6) - 50-70% weniger Traffic, Low Effort

### Pending (Medium Priority)
- [ ] Database Index on created_at (2.1) - Very Low Effort ⭐
- [ ] Transcript list virtualization (2.2)
- [ ] Lazy transcript content loading (3.3)

### Future
- [ ] Async database (3.1)
- [ ] PWA/Service Worker (3.2)

---

## Metrics to Track

| Metric | Current | Target |
|--------|---------|--------|
| Recording start latency | ~800ms | <300ms |
| Initial page load (LCP) | ~2.5s | <1.5s |
| Memory usage (100 transcripts) | ~15MB | <5MB |
| WebSocket messages/sec (recording) | ~60/s | <30/s |
| Bundle size (gzipped) | ~800KB | <400KB |

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
