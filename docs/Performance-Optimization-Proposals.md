# Performance Optimization Proposals

## Overview

This document outlines potential performance improvements for the Scriber application across backend, frontend, and architecture layers.

---

## Priority 1: High-Impact, Low-Effort

### 1.1 Database Connection Pooling

**Current State:**
- `database.py` creates a new SQLite connection for every database operation (`_get_connection()`)
- Each query opens and closes a connection

**Problem:**
- Connection overhead adds latency to every transcript load/save operation
- Not thread-safe under concurrent access

**Proposed Solution:**
```python
# Use a connection per thread (SQLite is not truly concurrent but this avoids repeated opens)
import threading

_local = threading.local()

def _get_connection() -> sqlite3.Connection:
    """Get or create a thread-local database connection."""
    if not hasattr(_local, 'conn') or _local.conn is None:
        _local.conn = sqlite3.connect(_DB_PATH, check_same_thread=False)
        _local.conn.row_factory = sqlite3.Row
    return _local.conn
```

**Impact:** ~10-50ms reduction per database operation
**Effort:** Low (1-2 hours)

---

### 1.2 Frontend Bundle Optimization

**Current State:**
- React 19 with Vite 7, full bundle loaded on initial page load
- Large dependencies: framer-motion, recharts, lucide-react (all icons), radix-ui

**Proposed Solution:**
1. **Code Splitting by Route:**
   ```typescript
   // Lazy load pages
   const Settings = lazy(() => import('./pages/Settings'));
   const Youtube = lazy(() => import('./pages/Youtube'));
   const FileTranscribe = lazy(() => import('./pages/FileTranscribe'));
   ```

2. **Tree-shake Lucide Icons:**
   ```typescript
   // Instead of: import { Settings, User, Home } from 'lucide-react'
   // Use individual imports in each component
   import Settings from 'lucide-react/dist/esm/icons/settings';
   ```

3. **Lazy Load Framer Motion:**
   - Only load `framer-motion` for pages that need animations
   - Use CSS transitions for simple micro-interactions

**Impact:** 30-50% reduction in initial bundle size
**Effort:** Medium (4-6 hours)

---

### 1.3 WebSocket Message Batching

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

### 2.1 Transcript List Virtualization

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

### Week 1: Quick Wins
- [ ] WebSocket message throttling (1.3)
- [x] Audio frame optimization (2.3) ✅
- [ ] Database connection caching (1.1)

### Week 2: Frontend Optimization
- [ ] Code splitting (1.2)
- [ ] Transcript list virtualization (2.1)

### Completed
- [x] STT/Analyzer caching (2.2) ✅
- [x] Audio frame optimization (2.3) ✅

### Future
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
