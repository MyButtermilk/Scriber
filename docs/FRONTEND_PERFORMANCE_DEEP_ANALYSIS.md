# Frontend Performance Tiefenanalyse

**Datum:** 2026-01-13  
**Autor:** Automatisierte Codebase-Analyse  
**Scope:** React Frontend (`Frontend/client/src/`)

**Status-Update 2026-06-01:** Diese Analyse ist historisch. Seitdem sind WebSocket-Singleton, mehrere memoized Cards, Route-Lazy-Loading für YouTube/File/Settings/TranscriptDetail, Intent-Prefetches und Backend-Pagination umgesetzt. Weiter offen sind Vite-Vendor-Chunking, echte Listen-Virtualisierung/Infinite Query und einige isolierte Re-Render-Themen.

---

## Executive Summary

Diese Analyse untersucht die gesamte Frontend-Architektur des Scriber-Projekts in der Tiefe und identifiziert Performance-Optimierungspotenziale in folgenden Kategorien:

| Kategorie | Kritisch | Hoch | Mittel | Niedrig |
|-----------|----------|------|--------|---------|
| React Re-Renders | 2 | 3 | 2 | 1 |
| State Management | 1 | 2 | 1 | - |
| Bundle & Lazy Loading | - | 2 | 2 | 1 |
| Animationen | - | 1 | 2 | 1 |
| Netzwerk & Caching | 1 | 1 | 2 | - |
| CSS & Rendering | - | 1 | 2 | 1 |

---

## 1. Architektur-Übersicht

### 1.1 Provider-Hierarchie

```
App.tsx
├── ThemeProvider (next-themes)
│   └── QueryClientProvider (TanStack Query)
│       └── BackendStatusProvider
│           └── WebSocketProvider (Singleton-Pattern ✓)
│               └── Toaster
│               └── BackendOfflineBanner
│               └── Router
```

**Positiv:** Die WebSocket-Verbindung ist als Singleton implementiert, was redundante Verbindungen verhindert.

**Problem:** Jede State-Änderung in `BackendStatusProvider` kann unnötige Re-Renders in allen Child-Komponenten auslösen.

### 1.2 Routing-Struktur

```
Router
├── /transcript/:id → TranscriptDetail (lazy loaded ✓)
├── / → LiveMic (eager loaded)
├── /youtube → Youtube (lazy loaded ✓)
├── /file → FileTranscribe (lazy loaded ✓)
└── /settings → Settings (lazy loaded ✓)
```

**Aktueller Stand:** LiveMic bleibt bewusst eager für die erste Ansicht; schwere Nebenseiten werden lazy geladen und teilweise auf Navigation-Intent vorab geladen. Vite meldet aber weiterhin einen initialen Chunk >500 kB, daher bleibt Vendor-Chunking offen.

---

## 2. Kritische Performance-Issues

### 2.1 SpeakerFormattedText - Dreifaches String-Parsing

**Datei:** `Frontend/client/src/pages/TranscriptDetail.tsx:32-102`  
**Schweregrad:** 🔴 KRITISCH

**Problem:**
```typescript
function SpeakerFormattedText({ content }: { content: string }) {
  const speakerPattern = /\[Speaker (\d+)\]:\s*/g;

  // 1. ERSTER PASS: test() konsumiert Regex
  if (!speakerPattern.test(content)) {
    return <span>{content}</span>;
  }

  // Reset notwendig
  speakerPattern.lastIndex = 0;

  // 2. ZWEITER PASS: while-loop mit exec()
  while ((match = speakerPattern.exec(content)) !== null) {
    const nextMatch = speakerPattern.exec(content); // ZUSÄTZLICHER exec() Aufruf!
    // ... segments Array wird gebaut, aber NIE verwendet!
  }

  // 3. DRITTER PASS: Split auf demselben Content
  const paragraphs = content.split(/\n\n+/);
  return <div>{paragraphs.map(...)}</div>;
}
```

**Impact:**
- 3 separate Durchläufe über den Content-String
- Das `segments` Array wird aufgebaut aber nie verwendet (toter Code)
- Jeder Parent-Re-Render führt alle Operationen erneut aus
- Bei 100KB Transcript: ~300KB String-Operationen pro Render

**Lösung:**
```typescript
const SpeakerFormattedText = React.memo(({ content }: { content: string }) => {
  const paragraphs = useMemo(() => {
    // SINGLE PASS: Split und Parse in einem Durchlauf
    return content.split(/\n\n+/).map(para => {
      const match = para.match(/^\[Speaker (\d+)\]:\s*([\s\S]*)$/);
      return match 
        ? { speaker: parseInt(match[1], 10), text: match[2] }
        : { speaker: null, text: para };
    });
  }, [content]);

  return <div className="space-y-4">{/* render paragraphs */}</div>;
});
```

**Geschätzte Verbesserung:** 60-70% weniger CPU-Zeit für Transcript-Rendering

---

### 2.2 FitText - DOM Thrashing

**Datei:** `Frontend/client/src/pages/TranscriptDetail.tsx:112-189`  
**Schweregrad:** 🔴 KRITISCH

**Problem:**
```typescript
function FitText({ children, ... }: FitTextProps) {
  const calculateFit = useCallback(() => {
    // DOM-Element erstellen
    const measureSpan = document.createElement('span');
    measureSpan.style.cssText = `...`;
    
    // → REFLOW #1: Einfügen in DOM
    document.body.appendChild(measureSpan);
    
    // → REFLOW #2: offsetWidth erzwingt Layout-Berechnung
    const textWidth = measureSpan.offsetWidth;
    
    // → REFLOW #3: Entfernen aus DOM
    document.body.removeChild(measureSpan);
    
    // State-Update triggert Re-Render
    setFontSize(newSize);
  }, [children, maxFontSize, minFontSize]);

  // ResizeObserver ruft calculateFit bei JEDEM Resize auf
  useEffect(() => {
    const resizeObserver = new ResizeObserver(() => {
      calculateFit(); // Synchroner Reflow!
    });
    resizeObserver.observe(container);
  }, [calculateFit]);
}
```

**Impact:**
- Mindestens 3 synchrone Reflows pro Berechnung
- Bei Window-Resize: Potenziell dutzende Reflows pro Sekunde
- Browser muss Layout für jeden appendChild/removeChild neu berechnen

**Lösung:**
```typescript
// Option A: Canvas-basierte Messung (EMPFOHLEN)
const measureTextWidth = (text: string, fontSize: number): number => {
  const canvas = document.createElement('canvas');
  const ctx = canvas.getContext('2d')!;
  ctx.font = `bold ${fontSize}px Inter`;
  return ctx.measureText(text).width;
};

// Option B: Persistentes Mess-Element (einmal erstellen, CSS-hidden)
const measureElement = useRef<HTMLSpanElement | null>(null);
useEffect(() => {
  if (!measureElement.current) {
    const span = document.createElement('span');
    span.style.cssText = 'position:absolute;visibility:hidden;white-space:nowrap';
    document.body.appendChild(span);
    measureElement.current = span;
  }
  return () => measureElement.current?.remove();
}, []);

// Option C: CSS container-queries (modern, kein JS nötig)
// container-type: inline-size; mit clamp(min, calc-vw, max)
```

**Geschätzte Verbesserung:** 80-90% weniger Layout-Thrashing

---

### 2.3 Timer State-Updates jede Sekunde

**Datei:** `Frontend/client/src/pages/TranscriptDetail.tsx:319-329`  
**Schweregrad:** 🟠 HOCH

**Problem:**
```typescript
// Timer läuft IMMER, auch wenn nicht im Processing-Status
useEffect(() => {
  const interval = setInterval(() => {
    if (startTimeRef.current && isProcessingRef.current) {
      const elapsed = Math.floor((Date.now() - startTimeRef.current) / 1000);
      setElapsedSeconds(elapsed); // ← STATE UPDATE jede Sekunde
    }
  }, 1000);
  return () => clearInterval(interval);
}, []);
```

**Impact:**
- 1 Re-Render pro Sekunde der gesamten TranscriptDetail-Komponente
- Alle Child-Komponenten werden re-evaluiert
- Timer läuft auch wenn Transcript gar nicht im Processing ist

**Lösung:**
```typescript
// Ref statt State + direktes DOM-Update
const elapsedSecondsRef = useRef(0);
const timerDisplayRef = useRef<HTMLSpanElement>(null);

useEffect(() => {
  if (transcript.status !== 'processing') return; // Nur bei Bedarf

  const interval = setInterval(() => {
    if (startTimeRef.current) {
      elapsedSecondsRef.current = Math.floor((Date.now() - startTimeRef.current) / 1000);
      if (timerDisplayRef.current) {
        timerDisplayRef.current.textContent = formatElapsed(elapsedSecondsRef.current);
      }
    }
  }, 1000);
  return () => clearInterval(interval);
}, [transcript.status]); // Abhängig vom Status
```

**Geschätzte Verbesserung:** Eliminiert ~60 unnötige Re-Renders pro Minute

---

## 3. State Management Issues

### 3.1 Settings.tsx - Monolithisches Component-Design

**Datei:** `Frontend/client/src/pages/Settings.tsx`  
**Schweregrad:** 🟠 HOCH

**Statistiken:**
- **1182 Zeilen** in einer einzigen Komponente
- **46 separate useState-Aufrufe**
- **18+ Handler-Funktionen**

**Auszug der State-Deklarationen:**
```typescript
const [openAIKey, setOpenAIKey] = useState("");
const [deepgramKey, setDeepgramKey] = useState("");
const [assemblyAIKey, setAssemblyAIKey] = useState("");
const [geminiKey, setGeminiKey] = useState("");
const [youtubeKey, setYoutubeKey] = useState("");
const [sonioxKey, setSonioxKey] = useState("");
const [elevenLabsKey, setElevenLabsKey] = useState("");
const [azureKey, setAzureKey] = useState("");
const [azureRegion, setAzureRegion] = useState("");
const [gladiaKey, setGladiaKey] = useState("");
const [groqKey, setGroqKey] = useState("");
const [awsKey, setAwsKey] = useState("");
// ... 34 weitere useState Aufrufe
```

**Impact:**
- Jede State-Änderung triggert Re-Render der gesamten 1182-Zeilen-Komponente
- Keine logische Trennung zwischen Sections (API Keys, Recording, Display)
- Jeder API-Key-Save ist eine separate API-Anfrage

**Lösung:**

```typescript
// 1. Consolidated State mit Reducer
interface SettingsState {
  apiKeys: Record<string, string>;
  recording: { hotkey: string; mode: string; micDevice: string };
  display: { visualizerBarCount: number; theme: string };
  // ...
}

const settingsReducer = (state: SettingsState, action: SettingsAction) => {/*...*/};
const [settings, dispatch] = useReducer(settingsReducer, initialState);

// 2. Component Splitting
// settings/
// ├── index.tsx (Layout + Accordion Container)
// ├── ApiKeysSection.tsx (memo)
// ├── RecordingSection.tsx (memo)
// ├── DisplaySection.tsx (memo)
// └── hooks/useSettingsSync.ts

// 3. Debounced Batch-Save
const savePendingChanges = useDebouncedCallback(
  () => updateSettings(pendingChanges),
  1000
);
```

---

### 3.2 Query Cache Konfiguration

**Datei:** `Frontend/client/src/lib/queryClient.ts:76-89`  
**Schweregrad:** 🟠 HOCH

**Problem:**
```typescript
export const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      refetchInterval: false,
      refetchOnWindowFocus: false,
      staleTime: Infinity, // ← Daten werden NIE als stale markiert
      retry: false,
    },
  },
});
```

**Impact:**
- Daten werden nie automatisch aktualisiert
- User sieht veraltete Daten bis zur manuellen Navigation
- Erfordert explizite `invalidateQueries` für jede Änderung
- Kein Window-Focus-Refresh (User kehrt zur App zurück → alte Daten)

**Empfohlene Lösung:**
```typescript
export const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      staleTime: 30_000, // 30 Sekunden
      gcTime: 10 * 60 * 1000, // 10 Minuten Cache
      refetchOnWindowFocus: true,
      refetchOnReconnect: true,
      retry: 1,
    },
  },
});

// Für bestimmte Queries (z.B. Settings) individuell überschreiben
useQuery({
  queryKey: ['/api/settings'],
  staleTime: 5 * 60 * 1000, // Settings ändern sich selten
});
```

---

### 3.3 Overly Broad Query Invalidations

**Dateien:** `LiveMic.tsx`, `Youtube.tsx`  
**Schweregrad:** 🟡 MITTEL

**Problem:**
```typescript
// LiveMic.tsx
queryClient.invalidateQueries({ queryKey: ['/api/transcripts'] });
// Invalidiert ALLE Transcript-Typen, nicht nur "mic"

// Youtube.tsx
queryClient.refetchQueries({ queryKey: ['/api/transcripts'] });
// Refetcht auch wenn nur YouTube-Liste geändert wurde
```

**Lösung:**
```typescript
// Zielgerichtete Invalidation
queryClient.invalidateQueries({
  queryKey: ['/api/transcripts', { type: 'mic' }],
});

// Oder: Optimistic Update ohne erneutes Fetchen
queryClient.setQueryData(
  ['/api/transcripts', { type: 'mic' }],
  (old) => old ? [...old, newTranscript] : [newTranscript]
);
```

---

## 4. Bundle & Lazy Loading

### 4.1 Eager Loading aller Haupt-Pages

**Datei:** `Frontend/client/src/App.tsx:12-17`  
**Schweregrad:** 🟡 MITTEL

**Aktueller Stand:**
```typescript
// Alle eager loaded
import LiveMic from "@/pages/LiveMic";
import Youtube from "@/pages/Youtube";
import FileTranscribe from "@/pages/FileTranscribe";
import Settings from "@/pages/Settings"; // 54KB!

// Nur diese lazy loaded
const TranscriptDetail = lazy(() => import("@/pages/TranscriptDetail"));
const NotFound = lazy(() => import("@/pages/not-found"));
```

**Bundle-Impact:**
| Seite | Größe | Laden |
|-------|-------|-------|
| Settings.tsx | 54KB | Eager |
| Youtube.tsx | 26KB | Eager |
| TranscriptDetail.tsx | 22KB | Lazy ✓ |
| FileTranscribe.tsx | 19KB | Eager |
| LiveMic.tsx | 18KB | Eager |

**Empfehlung:**
```typescript
// Settings lazy laden (größte Page, nicht immer benötigt)
const Settings = lazy(() => import("@/pages/Settings"));

// Alternativ: Route-basiertes Prefetching
const Settings = lazy(() => {
  // Prefetch wenn User in Richtung Settings navigiert
  return import("@/pages/Settings");
});
```

### 4.2 Schwere Abhängigkeiten

**Datei:** `Frontend/package.json`

| Dependency | Größe (ungefähr) | Nutzung |
|------------|------------------|---------|
| framer-motion | ~50KB gzip | Page Transitions, Animationen |
| react-markdown | ~15KB gzip | Nur in TranscriptDetail |
| recharts | ~40KB gzip | Potenziell ungenutzt |
| date-fns | ~10KB gzip | Datum-Formatierung |
| 58x Radix UI | ~80KB gzip | UI Components |

**Empfehlungen:**
```typescript
// react-markdown nur in TranscriptDetail dynamisch importieren
const ReactMarkdown = lazy(() => import('react-markdown'));

// recharts entfernen falls ungenutzt

// date-fns: nur benötigte Funktionen importieren
import { format } from 'date-fns/format';
// statt: import { format } from 'date-fns';
```

---

## 5. Animationen Performance

### 5.1 Framer Motion Page Transitions

**Datei:** `Frontend/client/src/components/layout/AppLayout.tsx:83-97`

**Aktueller Stand:**
```typescript
<AnimatePresence mode="wait">
  <motion.div
    key={currentKey}
    initial={{ opacity: 0 }}
    animate={{ opacity: 1 }}
    exit={{ opacity: 0 }}
    transition={{
      duration: 0.15,
      ease: "easeOut"
    }}
  >
    {children}
  </motion.div>
</AnimatePresence>
```

**Bewertung:** ✓ Bereits optimiert mit kurzer Duration (150ms)

**Mögliche Verbesserung:**
```typescript
transition={{
  duration: 0.12,
  ease: [0.4, 0, 0.2, 1], // CSS ease-out curve (GPU-optimiert)
}}
```

### 5.2 List Stagger Animations

**Datei:** `Frontend/client/src/pages/LiveMic.tsx:32-40`

```typescript
<motion.div
  initial={{ opacity: 0, y: 20 }}
  animate={{ opacity: 1, y: 0 }}
  transition={{
    delay: Math.min(index * 0.02, 0.1), // Max 100ms delay ✓
    duration: 0.2,
    ease: "easeOut"
  }}
>
```

**Bewertung:** ✓ Bereits optimiert mit cap bei 100ms

### 5.3 CSS Neumorphismus Overhead

**Datei:** `Frontend/client/src/index.css:103-117`

**Komplexe Box-Shadows:**
```css
--neu-raised-2:
  -10px -10px 22px var(--neu-shadow-light-strong),
  12px 12px 26px var(--neu-shadow-dark-strong);

--neu-inset-1:
  inset 4px 4px 10px var(--neu-shadow-dark),
  inset -4px -4px 10px var(--neu-shadow-light);
```

**Impact:**
- Multiple Box-Shadows auf vielen Elementen
- Hover-Transitions auf Shadows können Paint-intensive sein
- `::before` Pseudo-Elemente für zusätzliche Effekte

**Optimierungen:**
```css
/* GPU-Beschleunigung für animierte Elemente */
.neu-recording-row {
  will-change: transform;
  /* Schatten-Transition nur auf transform, nicht auf box-shadow */
  transition: transform 0.12s ease-out;
}

.neu-recording-row:hover {
  transform: translateY(-1px) scale(1.01);
  /* Keine box-shadow Änderung im Hover */
}

/* Reduzierte Motion für Accessibility */
@media (prefers-reduced-motion: reduce) {
  .neu-recording-row {
    transition: none !important;
  }
}
```

---

## 6. Netzwerk & Caching

### 6.1 WebSocket Singleton (Positiv ✓)

**Datei:** `Frontend/client/src/contexts/WebSocketContext.tsx`

```typescript
/**
 * PERFORMANCE OPTIMIZATION:
 * - Single WebSocket connection instead of 5+ per page
 * - Reduces server load and network overhead
 * - Eliminates connection setup latency when switching pages
 * - 200-400ms latency reduction on page navigation
 */
```

**Bewertung:** ✓ Best Practice implementiert

### 6.2 Prefetching auf Nav Hover

**Datei:** `Frontend/client/src/components/layout/AppLayout.tsx:19-22`

```typescript
const handleNavHover = () => {
  queryClient.prefetchQuery({ queryKey: ['/api/transcripts'] });
};
```

**Bewertung:** ✓ Gutes Pattern, aber könnte erweitert werden:

```typescript
const handleNavHover = (href: string) => {
  // Spezifisches Prefetching pro Route
  if (href === '/youtube') {
    queryClient.prefetchQuery({ queryKey: ['/api/transcripts', { type: 'youtube' }] });
  } else if (href === '/settings') {
    queryClient.prefetchQuery({ queryKey: ['/api/settings'] });
    queryClient.prefetchQuery({ queryKey: ['/api/microphones'] });
  }
};
```

### 6.3 Fehlende Request Deduplication

**Problem:** Mehrere Komponenten können gleichzeitig dieselben Daten fetchen.

**Lösung:** TanStack Query wird automatisch deduplizieren, aber staleTime muss > 0 sein:

```typescript
// Mit staleTime: Infinity dedupliziert Query nicht korrekt
// bei schnellen Re-Mounts
staleTime: 5000, // 5 Sekunden minimum
```

---

## 7. Kritische Callbacks ohne Memoization

### 7.1 deleteTranscript Callback

**Datei:** `Frontend/client/src/pages/Youtube.tsx:388-398`

**Problem:**
```typescript
const deleteTranscript = useCallback(async (e: React.MouseEvent, id: string) => {
  e.stopPropagation();
  if (deletingId) return; // State in Dependencies!
  setDeletingId(id);
  // ...
}, [deletingId, toast]); // Callback wird bei jeder deletingId-Änderung neu erstellt!
```

**Impact:**
- Memoisierte `YoutubeVideoCard` Komponenten re-rendern trotzdem
- Callback-Referenz ändert sich bei jedem Löschvorgang

**Lösung:**
```typescript
const deletingIdRef = useRef<string | null>(null);

const deleteTranscript = useCallback(async (e: React.MouseEvent, id: string) => {
  e.stopPropagation();
  if (deletingIdRef.current) return;
  deletingIdRef.current = id;
  setDeletingId(id); // State nur für UI-Update
  // ...
  deletingIdRef.current = null;
}, [toast]); // Stabile Dependencies
```

---

## 8. Prioritäts-Matrix

| Priorität | Issue | Kategorie | Geschätzter Aufwand | Impact |
|-----------|-------|-----------|---------------------|--------|
| **P0** | SpeakerFormattedText 3x Parsing | Algorithm | 2h | Sehr Hoch |
| **P0** | FitText DOM Thrashing | DOM | 3h | Sehr Hoch |
| **P1** | Timer State Updates | React | 1h | Hoch |
| **P1** | Settings 46 useState | Architecture | 4h | Hoch |
| **P1** | staleTime: Infinity | Caching | 30min | Hoch |
| **P2** | Settings lazy loading | Bundle | 30min | Mittel |
| **P2** | Query Invalidations | Caching | 1h | Mittel |
| **P2** | CSS Shadow Transitions | CSS | 1h | Mittel |
| **P3** | Callback Memoization | React | 1h | Niedrig |
| **P3** | Route-spezifisches Prefetching | UX | 1h | Niedrig |

---

## 9. Empfohlene Implementierungs-Reihenfolge

### Phase 1: Quick Wins (2-3 Stunden)
1. ✅ `staleTime` auf 30 Sekunden setzen
2. ✅ Timer von State auf Ref+DOM umstellen
3. ✅ Settings.tsx lazy loading aktivieren

### Phase 2: Algorithmus-Fixes (5-6 Stunden)
1. ✅ SpeakerFormattedText komplett neu schreiben
2. ✅ FitText auf Canvas-Messung umstellen

### Phase 3: Architektur (6-8 Stunden)
1. ✅ Settings.tsx in Sub-Komponenten aufteilen
2. ✅ Query Invalidation granularer gestalten
3. ✅ Callback-Dependencies stabilisieren

### Phase 4: Polish (2-3 Stunden)
1. ✅ CSS Shadow-Animationen optimieren
2. ✅ Route-spezifisches Prefetching
3. ✅ React DevTools Profiler Baseline erstellen

---

## 10. Monitoring-Empfehlungen

### React Profiler Integration

```typescript
// In App.tsx für Development
import { Profiler } from 'react';

const onRenderCallback = (
  id: string,
  phase: "mount" | "update",
  actualDuration: number
) => {
  if (actualDuration > 16) { // > 1 Frame (60fps)
    console.warn(`[Perf] Slow render: ${id} took ${actualDuration.toFixed(2)}ms`);
  }
};

<Profiler id="TranscriptDetail" onRender={onRenderCallback}>
  <TranscriptDetail />
</Profiler>
```

### Web Vitals Tracking

```typescript
// In main.tsx
import { onCLS, onFID, onLCP } from 'web-vitals';

onCLS(console.log);
onFID(console.log);
onLCP(console.log);
```

---

## Appendix: Positive Patterns bereits implementiert

✅ **WebSocket Singleton** - Vermeidet redundante Verbindungen  
✅ **Memoized List Cards** - `YoutubeVideoCard` und `TranscriptCard` mit `React.memo`  
✅ **Capped Animation Delays** - Max 100ms für Stagger-Effekte  
✅ **Prefetch on Hover** - Transcripts werden beim Nav-Hover vorgeladen  
✅ **Lazy Loading** - TranscriptDetail wird lazy geladen  
✅ **Reduced Motion Support** - CSS-Animationen respektieren System-Präferenz  
✅ **Exponential Backoff** - WebSocket Reconnection mit max 30s Delay  
