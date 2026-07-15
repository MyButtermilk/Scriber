import { apiUrl } from "@/lib/backend";
import {
  REST_API_VERSION,
  type FrontendLongTaskEntry,
  type FrontendPerformanceReportRequest,
} from "@/lib/api-types";

const LONG_TASK_THRESHOLD_MS = 200;
const MAX_PENDING_ENTRIES = 64;
// Long tasks are rare and the benchmark window may close shortly after a
// native shell interaction. Dispatch on the next task instead of hiding the
// evidence behind a polling interval or a long debounce.
const REPORT_DEBOUNCE_MS = 0;
const RETRY_DELAY_MS = 1_000;
const MAX_RETRY_ATTEMPTS = 5;

let observer: PerformanceObserver | null = null;
let reportingEnabled = false;
let flushTimer: number | null = null;
let flushChain: Promise<void> = Promise.resolve();
let sequence = 0;
let droppedEntries = 0;
let pendingEntries: FrontendLongTaskEntry[] = [];
let sourceInstanceId = "";
let windowStartedAtMs = 0;
let observerSupported = false;
let consecutiveFailures = 0;
let pendingHeartbeatSequence = 0;

function newSourceInstanceId(): string {
  if (typeof crypto !== "undefined" && typeof crypto.randomUUID === "function") {
    return crypto.randomUUID();
  }
  return `${Date.now().toString(36)}-${Math.random().toString(36).slice(2)}`;
}

function scheduleFlush(delayMs = REPORT_DEBOUNCE_MS): void {
  if (!reportingEnabled || flushTimer !== null || typeof window === "undefined") {
    return;
  }
  flushTimer = window.setTimeout(() => {
    flushTimer = null;
    void flushFrontendPerformanceReport();
  }, delayMs);
}

function retainFailedBatch(entries: FrontendLongTaskEntry[]): void {
  const combined = [...entries, ...pendingEntries];
  if (combined.length > MAX_PENDING_ENTRIES) {
    droppedEntries += combined.length - MAX_PENDING_ENTRIES;
  }
  pendingEntries = combined.slice(-MAX_PENDING_ENTRIES);
}

function collectLongTaskEntries(entries: PerformanceEntry[]): void {
  for (const entry of entries) {
    if (entry.duration <= LONG_TASK_THRESHOLD_MS || entry.startTime < windowStartedAtMs) {
      continue;
    }
    sequence += 1;
    const item = {
      sequence,
      startTimeMs: Math.round(entry.startTime * 1000) / 1000,
      durationMs: Math.round(entry.duration * 1000) / 1000,
    };
    if (pendingEntries.length >= MAX_PENDING_ENTRIES) {
      pendingEntries.shift();
      droppedEntries += 1;
    }
    pendingEntries.push(item);
  }
}

function drainObserverRecords(): void {
  if (observer) {
    collectLongTaskEntries(observer.takeRecords());
  }
}

async function performFrontendPerformanceFlush(): Promise<void> {
  drainObserverRecords();
  const heartbeatSequence = pendingHeartbeatSequence;
  const entries = pendingEntries;
  pendingEntries = [];
  const payload: FrontendPerformanceReportRequest = {
    apiVersion: REST_API_VERSION,
    sourceInstanceId,
    observerSupported,
    windowStartedAtMs,
    observedAtMs: performance.now(),
    // Cumulative for retry idempotency: a response lost after backend commit
    // must not make the same dropped-entry count get added twice.
    droppedEntries,
    heartbeatSequence,
    entries,
  };

  try {
    const response = await fetch(apiUrl("/api/runtime/frontend-performance"), {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
      keepalive: true,
    });
    if (!response.ok) {
      retainFailedBatch(entries);
      consecutiveFailures += 1;
      if (consecutiveFailures < MAX_RETRY_ATTEMPTS) {
        scheduleFlush(RETRY_DELAY_MS * (2 ** (consecutiveFailures - 1)));
      }
    } else {
      consecutiveFailures = 0;
      if (pendingHeartbeatSequence <= heartbeatSequence) {
        pendingHeartbeatSequence = 0;
      }
    }
  } catch {
    retainFailedBatch(entries);
    consecutiveFailures += 1;
    if (consecutiveFailures < MAX_RETRY_ATTEMPTS) {
      scheduleFlush(RETRY_DELAY_MS * (2 ** (consecutiveFailures - 1)));
    }
  } finally {
    if (
      (pendingEntries.length > 0 || pendingHeartbeatSequence > 0)
      && consecutiveFailures === 0
    ) {
      scheduleFlush();
    }
  }
}

export function flushFrontendPerformanceReport(
  heartbeatSequence = 0,
  expectedSourceInstanceId = "",
): Promise<void> {
  if (
    !reportingEnabled
    || !sourceInstanceId
    || (expectedSourceInstanceId && expectedSourceInstanceId !== sourceInstanceId)
  ) {
    return Promise.resolve();
  }
  if (heartbeatSequence > 0) {
    pendingHeartbeatSequence = Math.max(pendingHeartbeatSequence, heartbeatSequence);
  }
  drainObserverRecords();
  // A synchronous observer/browser failure must not poison the serialization
  // chain forever: the next explicit heartbeat still gets one bounded chance
  // to drain and report its records.
  flushChain = flushChain.then(
    performFrontendPerformanceFlush,
    performFrontendPerformanceFlush,
  );
  return flushChain;
}

export function setFrontendPerformanceReportingEnabled(enabled: boolean): void {
  reportingEnabled = enabled;
  if (enabled) {
    consecutiveFailures = 0;
    scheduleFlush(0);
  } else if (flushTimer !== null && typeof window !== "undefined") {
    window.clearTimeout(flushTimer);
    flushTimer = null;
  }
}

export function startFrontendLongTaskObserver(): () => void {
  if (typeof window === "undefined" || observer !== null || sourceInstanceId) {
    return () => undefined;
  }

  sourceInstanceId = newSourceInstanceId();
  windowStartedAtMs = performance.now();
  try {
    observerSupported = typeof PerformanceObserver !== "undefined"
      && Array.isArray(PerformanceObserver.supportedEntryTypes)
      && PerformanceObserver.supportedEntryTypes.includes("longtask");
  } catch {
    observerSupported = false;
  }

  if (observerSupported) {
    try {
      const candidate = new PerformanceObserver((list) => {
        collectLongTaskEntries(list.getEntries());
        if (pendingEntries.length > 0) {
          scheduleFlush();
        }
      });
      observer = candidate;
      candidate.observe({ type: "longtask" });
    } catch {
      observer?.disconnect();
      observer = null;
      observerSupported = false;
    }
  }

  return () => {
    void flushFrontendPerformanceReport();
    observer?.disconnect();
    observer = null;
    setFrontendPerformanceReportingEnabled(false);
  };
}
