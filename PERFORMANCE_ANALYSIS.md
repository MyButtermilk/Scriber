# Performance Analysis Report: Scriber Codebase

**Date:** 2026-01-12
**Analysis Scope:** React Frontend + Python Backend
**Categories Analyzed:** N+1 Queries, React Re-renders, Algorithm Efficiency, State Management

---

## Executive Summary

This analysis identified **12 significant performance issues** across the Scriber codebase, ranging from critical algorithmic inefficiencies to suboptimal React patterns. The most impactful issues include:

1. **Double-parsing regex in transcript formatting** (CRITICAL)
2. **Full content search without pagination** (CRITICAL)
3. **Missing database indexes** (HIGH)
4. **Unnecessary re-renders every second** (HIGH)
5. **Overly broad cache invalidations** (MEDIUM)

---

## 1. UNNECESSARY REACT RE-RENDERS

### 1.1 SpeakerFormattedText Component - Missing React.memo
**Location:** `Frontend/client/src/pages/TranscriptDetail.tsx:33-102`
**Severity:** HIGH

**Issue:**
The `SpeakerFormattedText` component is not memoized and performs expensive regex parsing on every render, even when content hasn't changed.

**Code:**
```typescript
const SpeakerFormattedText = ({ content }: { content: string }) => {
  const speakerPattern = /\[Speaker (\d+)\]:\s*/g;

  if (!speakerPattern.test(content)) {
    return <span>{content}</span>;
  }

  speakerPattern.lastIndex = 0;
  // ... complex regex parsing on every render
```

**Impact:**
- Component re-renders on every parent state change
- O(n) string operations on potentially large transcript content
- No memoization of parsed results

**Recommendation:**
```typescript
const SpeakerFormattedText = React.memo(({ content }: { content: string }) => {
  const segments = useMemo(() => {
    // Parse once and cache result
  }, [content]);

  return <>{segments}</>;
});
```

---

### 1.2 FitText Component - DOM Thrashing
**Location:** `Frontend/client/src/pages/TranscriptDetail.tsx:112-189`
**Severity:** MEDIUM

**Issue:**
Creates temporary DOM elements on every font size calculation, causing layout thrashing.

**Code:**
```typescript
const measureSpan = document.createElement('span');
measureSpan.style.cssText = `...`;
document.body.appendChild(measureSpan);      // DOM insertion
const textWidth = measureSpan.offsetWidth;   // Forces reflow
document.body.removeChild(measureSpan);      // DOM removal
```

**Impact:**
- Multiple synchronous reflows per calculation
- Browser must recalculate layout for each insertion/removal
- Performance degradation with frequent resizes

**Recommendation:**
- Use `canvas.measureText()` for text measurement
- Or create persistent measurement element (hidden with CSS)
- Consider using CSS `container-query` for responsive sizing

---

### 1.3 TranscriptDetail Timer - Unnecessary State Updates
**Location:** `Frontend/client/src/pages/TranscriptDetail.tsx:320-329`
**Severity:** HIGH

**Issue:**
Timer updates `elapsedSeconds` state every second, causing full component re-renders.

**Code:**
```typescript
useEffect(() => {
  const interval = setInterval(() => {
    if (startTimeRef.current && isProcessingRef.current) {
      setElapsedSeconds(elapsed);  // STATE UPDATE every second
    }
  }, 1000);
  return () => clearInterval(interval);
}, []); // Runs unconditionally
```

**Impact:**
- 1 unnecessary re-render per second
- All child components re-evaluate props
- Timer runs even when not needed

**Recommendation:**
```typescript
// Use ref for display value, only update when needed
const elapsedRef = useRef(0);

// Update DOM directly without state
useEffect(() => {
  if (!isProcessing) return;

  const interval = setInterval(() => {
    if (timerElementRef.current) {
      timerElementRef.current.textContent = formatElapsed();
    }
  }, 1000);

  return () => clearInterval(interval);
}, [isProcessing]);
```

---

### 1.4 Callback Dependencies Break Memoization
**Location:** `Frontend/client/src/pages/Youtube.tsx:388-398`
**Severity:** MEDIUM

**Issue:**
Callbacks include state variables in dependencies, recreating on every state change and breaking child component memoization.

**Code:**
```typescript
const deleteTranscript = useCallback(async (e: React.MouseEvent, id: string) => {
  e.stopPropagation();
  if (deletingId) return;  // State in dependency array
  setDeletingId(id);
  // ... deletion logic
}, [deletingId, toast]);  // Recreates when deletingId changes!
```

**Impact:**
- Memoized `YoutubeVideoCard` components re-render unnecessarily
- Callback recreation triggers prop comparison failures
- Similar issues in `LiveMic.tsx:290-318`

**Recommendation:**
```typescript
const deletingIdRef = useRef<string | null>(null);

const deleteTranscript = useCallback(async (e: React.MouseEvent, id: string) => {
  e.stopPropagation();
  if (deletingIdRef.current) return;  // Use ref instead
  deletingIdRef.current = id;
  // ...
}, [toast]);  // Stable dependency list
```

---

## 2. DATABASE N+1 QUERY PATTERNS

### 2.1 Full Content Search Without Pagination
**Location:** `src/web_api.py:1316-1350`
**Severity:** CRITICAL

**Issue:**
`list_transcripts()` loads ALL records and searches full content for every query, with no pagination or limits.

**Code:**
```python
def list_transcripts(self, *, include_content: bool = False, query: str = "", transcript_type: str = ""):
    out = []
    query_lower = query.lower().strip() if query else ""

    for rec in self._history:  # Iterates ALL records
        if transcript_type and rec.type != transcript_type:
            continue
        if query_lower:
            searchable = (
                (rec.title or "").lower() +
                (rec.content or "").lower() +      # Full transcript!
                (rec.channel or "").lower() +
                (rec.summary or "").lower()        # Full summary!
            )
            if query_lower not in searchable:
                continue
        out.append(rec.to_public(include_content=include_content))
    return out
```

**Impact:**
- For 100 transcripts × 10KB average = 1MB of string operations
- No early exit from search
- Scales poorly: O(n × m) where n=records, m=content size
- Frontend requests like `GET /api/transcripts?type=mic` load entire history

**Recommendation:**
```python
def list_transcripts(
    self,
    *,
    include_content: bool = False,
    query: str = "",
    transcript_type: str = "",
    limit: int = 50,          # Add pagination
    offset: int = 0
):
    # 1. Add database indexes (see section 2.2)
    # 2. Search title/channel first (early exit)
    # 3. Only search content if explicitly requested
    # 4. Apply LIMIT/OFFSET in SQL query
```

---

### 2.2 Missing Database Indexes
**Location:** `src/database.py:63-86`
**Severity:** HIGH

**Issue:**
No indexes on frequently queried columns: `type`, `status`, `created_at`.

**Code:**
```python
def init_database() -> None:
    with _get_connection() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS transcripts (
                id TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                type TEXT NOT NULL,        -- No index!
                status TEXT NOT NULL,      -- No index!
                created_at TEXT NOT NULL,  -- No index!
                ...
            )
        """)
        conn.commit()  # No indexes created
```

**Impact:**
- Queries with `WHERE type = 'mic'` do full table scan
- Sorting by `created_at` scans entire table
- Filtering by status requires checking every row

**Recommendation:**
```python
def init_database() -> None:
    with _get_connection() as conn:
        # ... create table ...

        # Add indexes
        conn.execute("CREATE INDEX IF NOT EXISTS idx_type ON transcripts(type)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_status ON transcripts(status)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_created_at ON transcripts(created_at)")

        # Composite index for filtered + sorted queries
        conn.execute("CREATE INDEX IF NOT EXISTS idx_type_created ON transcripts(type, created_at DESC)")

        conn.commit()
```

---

### 2.3 Overly Broad Query Invalidations
**Location:** `Frontend/client/src/pages/LiveMic.tsx:220-230`, `Youtube.tsx:250`
**Severity:** MEDIUM

**Issue:**
Cache invalidations affect all transcript queries instead of targeted updates.

**Code:**
```typescript
// LiveMic.tsx:303
queryClient.invalidateQueries({ queryKey: ["/api/transcripts"] });
// Invalidates ALL transcript types, not just "mic"

// Youtube.tsx:250
queryClient.refetchQueries({ queryKey: ["/api/transcripts"] });
// Refetches even when only YouTube list changed
```

**Impact:**
- Changes to one transcript type trigger refetch of all types
- Unnecessary network requests
- Race conditions between multiple invalidations

**Recommendation:**
```typescript
// More targeted invalidation
queryClient.invalidateQueries({
  queryKey: ["/api/transcripts", { type: "mic" }]
});

// Or update cache directly
queryClient.setQueryData(
  ["/api/transcripts", { type: "mic" }],
  (old) => [...old, newTranscript]
);
```

---

## 3. INEFFICIENT ALGORITHMS

### 3.1 Double-Parsing Regex in SpeakerFormattedText
**Location:** `Frontend/client/src/pages/TranscriptDetail.tsx:33-102`
**Severity:** CRITICAL

**Issue:**
Component performs regex parsing multiple times and creates unused data structures.

**Code:**
```typescript
// First: test() consumes regex
if (!speakerPattern.test(content)) {
  return <span>{content}</span>;
}

// Reset regex state
speakerPattern.lastIndex = 0;

// Second: exec() in loop
while ((match = speakerPattern.exec(content)) !== null) {
  const nextMatch = speakerPattern.exec(content);  // DOUBLE exec() call!
  // ... builds segments array ...
}

// Third: Split AGAIN on same content
const paragraphs = content.split(/\n\n+/);  // Re-parses everything!
```

**Impact:**
- 3 separate passes over content string
- `segments` array is built but never used
- O(n) operations repeated unnecessarily
- Executes on every component render

**Recommendation:**
```typescript
const SpeakerFormattedText = React.memo(({ content }: { content: string }) => {
  const paragraphs = useMemo(() => {
    // Single pass: split by paragraphs, check for speakers
    return content.split(/\n\n+/).map(para => {
      const match = para.match(/^\[Speaker (\d+)\]:\s*/);
      if (match) {
        return {
          speaker: match[1],
          text: para.slice(match[0].length)
        };
      }
      return { speaker: null, text: para };
    });
  }, [content]);

  return (
    <div>
      {paragraphs.map((p, i) => (
        <div key={i} className={p.speaker ? `speaker-${p.speaker}` : ''}>
          {p.text}
        </div>
      ))}
    </div>
  );
});
```

---

### 3.2 Linear Search for Transcripts
**Location:** `src/web_api.py:1352-1356`
**Severity:** LOW

**Issue:**
`get_transcript()` uses O(n) linear search instead of O(1) dictionary lookup.

**Code:**
```python
def get_transcript(self, transcript_id: str) -> Optional[dict[str, Any]]:
    for rec in self._history:  # O(n) linear search
        if rec.id == transcript_id:
            return rec.to_public(include_content=True)
    return None
```

**Impact:**
- Currently low (fast enough for <1000 transcripts)
- Will scale poorly with large datasets
- Unnecessary iteration

**Recommendation:**
```python
class TranscriptCache:
    def __init__(self):
        self._history = []
        self._history_dict = {}  # Add dictionary index

    def add_record(self, record):
        self._history.append(record)
        self._history_dict[record.id] = record

    def get_transcript(self, transcript_id: str):
        record = self._history_dict.get(transcript_id)  # O(1) lookup
        return record.to_public(include_content=True) if record else None
```

---

### 3.3 Full Content Concatenation in Search
**Location:** `src/web_api.py:1340-1347`
**Severity:** MEDIUM

**Issue:**
Search concatenates title + content + channel + summary for every record, no early exit.

**Code:**
```python
if query_lower:
    searchable = (
        (rec.title or "").lower() +      # String concatenation
        (rec.content or "").lower() +    # Full transcript content!
        (rec.channel or "").lower() +
        (rec.summary or "").lower()      # Full summary!
    )
    if query_lower not in searchable:
        continue
```

**Impact:**
- For 100 records × 10KB each = 1MB string operations
- No early exit if match found in title
- Memory allocation for large concatenated string

**Recommendation:**
```python
if query_lower:
    # Check fields in order of likelihood, with early exit
    if (query_lower in (rec.title or "").lower() or
        query_lower in (rec.channel or "").lower() or
        query_lower in (rec.summary or "").lower()[:500] or  # Check preview only
        query_lower in (rec.content or "").lower()[:1000]):  # Check preview only
        # Match found
        pass
    else:
        continue
```

---

## 4. STATE MANAGEMENT ANTI-PATTERNS

### 4.1 Settings Component - 40+ Individual State Variables
**Location:** `Frontend/client/src/pages/Settings.tsx:1-180`
**Severity:** HIGH

**Issue:**
Settings uses 40+ individual `useState` calls instead of consolidated state object.

**Code:**
```typescript
const [openAIKey, setOpenAIKey] = useState("");
const [deepgramKey, setDeepgramKey] = useState("");
const [assemblyAIKey, setAssemblyAIKey] = useState("");
const [geminiKey, setGeminiKey] = useState("");
const [anthropicKey, setAnthropicKey] = useState("");
// ... 35+ more state variables
```

**Impact:**
- Each state change causes re-render checking all 40+ states
- No single source of truth
- Difficult to manage related state updates
- Each key save triggers separate API call

**Recommendation:**
```typescript
const [settings, setSettings] = useState({
  apiKeys: {
    openai: "",
    deepgram: "",
    assemblyai: "",
    // ... etc
  },
  llm: {
    model: "",
    temperature: 0.7,
    // ... etc
  }
});

// Update single field
const updateApiKey = (provider: string, value: string) => {
  setSettings(prev => ({
    ...prev,
    apiKeys: { ...prev.apiKeys, [provider]: value }
  }));
};
```

---

### 4.2 Query Cache Configuration - staleTime: Infinity
**Location:** `Frontend/client/src/lib/queryClient.ts:76-89`
**Severity:** MEDIUM

**Issue:**
Query cache configured with `staleTime: Infinity` prevents automatic data refresh.

**Code:**
```typescript
export const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      refetchInterval: false,
      refetchOnWindowFocus: false,
      staleTime: Infinity,  // Data NEVER considered stale
      retry: false,
    },
  },
});
```

**Impact:**
- User sees stale data until manual navigation
- No refresh when returning to app
- Requires manual invalidations for every change

**Recommendation:**
```typescript
export const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      refetchOnWindowFocus: true,   // Refresh when user returns
      staleTime: 30000,              // 30 seconds
      gcTime: 10 * 60 * 1000,        // 10 minutes cache
      retry: 1,                      // Retry once on failure
    },
  },
});
```

---

## 5. PRIORITY MATRIX

| Priority | Issue | Category | Location | Estimated Fix Time |
|----------|-------|----------|----------|-------------------|
| **P0 - CRITICAL** | Double-parsing regex in SpeakerFormattedText | Algorithm | TranscriptDetail.tsx:33-102 | 2-3 hours |
| **P0 - CRITICAL** | Full content search without pagination | N+1 Pattern | web_api.py:1316-1350 | 3-4 hours |
| **P1 - HIGH** | Missing database indexes | Database | database.py:63-86 | 1 hour |
| **P1 - HIGH** | ElapsedSeconds timer state updates | React Render | TranscriptDetail.tsx:320-329 | 1 hour |
| **P1 - HIGH** | 40+ separate state variables in Settings | State Mgmt | Settings.tsx:1-180 | 2-3 hours |
| **P1 - HIGH** | FitText DOM thrashing | React | TranscriptDetail.tsx:112-189 | 2-3 hours |
| **P2 - MEDIUM** | Overly broad query invalidations | Cache | LiveMic.tsx, Youtube.tsx | 1-2 hours |
| **P2 - MEDIUM** | Callback dependencies break memoization | React | Youtube.tsx:388-398 | 1 hour |
| **P2 - MEDIUM** | staleTime: Infinity cache config | Cache | queryClient.ts:82 | 30 minutes |
| **P2 - MEDIUM** | Full content concatenation in search | Algorithm | web_api.py:1340-1347 | 1 hour |
| **P3 - LOW** | Linear search for transcripts | Algorithm | web_api.py:1352-1356 | 1 hour |

---

## 6. RECOMMENDED IMPLEMENTATION ORDER

### Phase 1: Quick Wins (4-5 hours)
1. Add database indexes (1 hour)
2. Fix staleTime configuration (30 min)
3. Fix timer to use ref instead of state (1 hour)
4. Improve query invalidations (1-2 hours)

### Phase 2: Algorithm Fixes (5-6 hours)
1. Rewrite SpeakerFormattedText parsing (2-3 hours)
2. Add pagination to list_transcripts (3-4 hours)

### Phase 3: Architecture Improvements (5-7 hours)
1. Consolidate Settings state (2-3 hours)
2. Fix FitText DOM operations (2-3 hours)
3. Add dictionary lookup for transcripts (1 hour)

### Phase 4: Polish (2-3 hours)
1. Fix callback memoization (1 hour)
2. Optimize search concatenation (1 hour)
3. Add monitoring/profiling (1 hour)

**Total Estimated Time:** 16-21 hours

---

## 7. MONITORING RECOMMENDATIONS

### Add Performance Metrics

```typescript
// Frontend: React Profiler
import { Profiler } from 'react';

<Profiler id="TranscriptDetail" onRender={(id, phase, actualDuration) => {
  if (actualDuration > 16) {  // Longer than 1 frame
    console.warn(`Slow render: ${id} took ${actualDuration}ms`);
  }
}}>
  <TranscriptDetail />
</Profiler>
```

### Backend: Add Query Timing

```python
import time

def list_transcripts(self, **kwargs):
    start = time.perf_counter()
    result = self._list_transcripts_impl(**kwargs)
    duration = time.perf_counter() - start

    if duration > 0.1:  # Slower than 100ms
        logger.warning(f"Slow query: list_transcripts took {duration:.2f}s")

    return result
```

---

## 8. TESTING STRATEGY

1. **Performance Regression Tests**
   - Benchmark current performance before changes
   - Add tests for large datasets (1000+ transcripts)
   - Measure render times for transcript detail page

2. **Load Testing**
   - Test with 10KB, 100KB, 1MB transcript content
   - Verify pagination handles large result sets
   - Check memory usage during long recording sessions

3. **React DevTools Profiler**
   - Profile components before/after optimizations
   - Verify memoization prevents re-renders
   - Check commit frequency during recording

---

## Conclusion

The Scriber codebase has several performance bottlenecks that can be addressed systematically:

- **CRITICAL issues** (double-parsing, full-content search) should be fixed immediately
- **HIGH priority** items (indexes, state management) provide significant impact
- **MEDIUM/LOW** items can be addressed during regular development

Most issues stem from:
1. Missing memoization in React components
2. Lack of pagination/indexing in database queries
3. Inefficient string operations on large content
4. Overly broad cache invalidation strategies

The recommended fixes are straightforward and can be implemented incrementally without major architectural changes.
