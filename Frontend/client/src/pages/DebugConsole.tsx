import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
} from "@/components/ui/alert-dialog";
import {
  AlertTriangle,
  ArrowDownToLine,
  Bug,
  CalendarDays,
  CheckCircle2,
  Circle,
  Clipboard,
  Download,
  Eraser,
  Eye,
  Filter,
  Check,
  Layers3,
  RefreshCw,
  Search,
  Terminal,
  Trash2,
} from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Switch } from "@/components/ui/switch";
import { RuntimeLogMessage } from "@/components/debug/RuntimeLogMessage";
import { useToast } from "@/hooks/use-toast";
import { apiUrl } from "@/lib/backend";
import { fetchWithTimeout } from "@/lib/fetch-with-timeout";
import { cn } from "@/lib/utils";
import type {
  PostProcessingDiagnostic,
  PostProcessingDiagnosticsResponse,
  RuntimeLogEntry,
  RuntimeLogsClearResponse,
  RuntimeLogsResponse,
} from "@/lib/api-types";

const LEVELS = ["ALL", "CRITICAL", "ERROR", "WARNING", "INFO", "DEBUG", "TRACE"] as const;
const ALL_DATES_VALUE = "all";

const levelStyles: Record<string, string> = {
  TRACE: "border-slate-300 bg-slate-100 text-slate-700 dark:border-slate-700 dark:bg-slate-900/40 dark:text-slate-300",
  DEBUG: "border-sky-300 bg-sky-100 text-sky-800 dark:border-sky-800 dark:bg-sky-950/45 dark:text-sky-200",
  INFO: "border-blue-300 bg-blue-100 text-blue-800 dark:border-blue-800 dark:bg-blue-950/45 dark:text-blue-200",
  SUCCESS: "border-emerald-300 bg-emerald-100 text-emerald-800 dark:border-emerald-800 dark:bg-emerald-950/45 dark:text-emerald-200",
  WARNING: "border-amber-300 bg-amber-100 text-amber-900 dark:border-amber-800 dark:bg-amber-950/50 dark:text-amber-200",
  ERROR: "border-red-300 bg-red-100 text-red-800 dark:border-red-800 dark:bg-red-950/50 dark:text-red-200",
  CRITICAL: "border-red-400 bg-red-200 text-red-950 dark:border-red-700 dark:bg-red-950 dark:text-red-100",
};

const rowStyles: Record<string, string> = {
  TRACE: "border-l-slate-300 bg-slate-500/5",
  DEBUG: "border-l-sky-400 bg-sky-500/5",
  INFO: "border-l-blue-400",
  SUCCESS: "border-l-emerald-400 bg-emerald-500/5",
  WARNING: "border-l-amber-400 bg-amber-500/10",
  ERROR: "border-l-red-500 bg-red-500/10",
  CRITICAL: "border-l-red-600 bg-red-500/15",
};

function iconForLevel(level: string) {
  if (level === "ERROR" || level === "CRITICAL") return AlertTriangle;
  if (level === "SUCCESS") return CheckCircle2;
  if (level === "DEBUG" || level === "TRACE") return Bug;
  return Circle;
}

function normalizeLevel(level: string) {
  const value = (level || "INFO").toUpperCase();
  if (value === "WARN") return "WARNING";
  if (value === "ERR" || value === "FATAL") return value === "FATAL" ? "CRITICAL" : "ERROR";
  return value;
}

function formatEntryTime(entry: RuntimeLogEntry) {
  if (entry.timestampMs) {
    return new Date(entry.timestampMs).toLocaleTimeString("de-DE", {
      hour: "2-digit",
      minute: "2-digit",
      second: "2-digit",
    });
  }
  const parsed = timestampToDate(entry.timestamp);
  if (parsed) {
    return parsed.toLocaleTimeString("de-DE", {
      hour: "2-digit",
      minute: "2-digit",
      second: "2-digit",
    });
  }
  return entry.timestamp || "";
}

function formatLogLine(entry: RuntimeLogEntry) {
  const level = normalizeLevel(entry.level);
  const component = entry.component ? ` [${entry.component}]` : "";
  return `${formatEntryTime(entry).padEnd(12)} ${level.padEnd(8)} ${entry.source}:${entry.line}${component} ${entry.message}`;
}

function dateInputValue(date: Date) {
  const year = date.getFullYear();
  const month = String(date.getMonth() + 1).padStart(2, "0");
  const day = String(date.getDate()).padStart(2, "0");
  return `${year}-${month}-${day}`;
}

function timestampToDate(timestamp: string | null | undefined) {
  const value = (timestamp || "").trim();
  if (!value) return null;

  const germanDate = value.match(/^(\d{1,2})\.(\d{1,2})\.(\d{4})(?:[,\s]+(\d{1,2}):(\d{2})(?::(\d{2}))?)?/);
  if (germanDate) {
    const [, day, month, year, hour = "0", minute = "0", second = "0"] = germanDate;
    return new Date(Number(year), Number(month) - 1, Number(day), Number(hour), Number(minute), Number(second));
  }

  const isoLike = value.match(/\d{4}-\d{2}-\d{2}(?:[ T]\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?(?:Z|[+-]\d{2}:?\d{2})?)?/);
  if (isoLike) {
    const normalized = isoLike[0].includes("T") ? isoLike[0] : isoLike[0].replace(" ", "T");
    const parsed = new Date(normalized);
    if (!Number.isNaN(parsed.getTime())) return parsed;
  }

  const timeOnly = value.match(/^(\d{1,2}):(\d{2})(?::(\d{2})(?:\.\d+)?)?/);
  if (timeOnly) {
    const today = new Date();
    today.setHours(Number(timeOnly[1]), Number(timeOnly[2]), Number(timeOnly[3] || "0"), 0);
    return today;
  }

  const parsedMs = Date.parse(value);
  if (!Number.isNaN(parsedMs)) return new Date(parsedMs);
  return null;
}

function entryDateKey(entry: RuntimeLogEntry) {
  if (entry.timestampMs) {
    return dateInputValue(new Date(entry.timestampMs));
  }
  const parsed = timestampToDate(entry.timestamp);
  return parsed ? dateInputValue(parsed) : "";
}

function entrySortValue(entry: RuntimeLogEntry, fallbackIndex: number) {
  if (entry.timestampMs) return entry.timestampMs;
  const parsed = timestampToDate(entry.timestamp);
  if (parsed) return parsed.getTime();
  return fallbackIndex;
}

function logEntryKey(entry: RuntimeLogEntry) {
  return [
    entry.source,
    entry.line,
    entry.timestamp || "",
    entry.timestampMs || "",
    normalizeLevel(entry.level),
    entry.component || "",
    entry.message,
  ].join("\u001f");
}

function filenameFromDisposition(disposition: string | null) {
  const match = disposition?.match(/filename="?([^";]+)"?/i);
  return match?.[1] || `scriber-support-bundle-${Date.now()}.zip`;
}

function formatMs(value: number | null | undefined) {
  if (typeof value !== "number" || !Number.isFinite(value)) return "--";
  if (value >= 1000) return `${(value / 1000).toFixed(2)} s`;
  return `${Math.round(value)} ms`;
}

function postProcessingStatusLabel(item: PostProcessingDiagnostic | null | undefined) {
  if (!item) return "No runs";
  if (item.status === "success") return "Last run succeeded";
  if (item.status === "failure") return "Last run failed";
  if (item.status === "empty_output") return "Empty output";
  if (item.status === "skipped") return "Skipped";
  return item.status || "Unknown";
}

export default function DebugConsole() {
  const { toast } = useToast();
  const defaultDateFilter = useMemo(() => dateInputValue(new Date()), []);
  const [logs, setLogs] = useState<RuntimeLogEntry[]>([]);
  const [sources, setSources] = useState<string[]>([]);
  const [postProcessingDiagnostics, setPostProcessingDiagnostics] = useState<PostProcessingDiagnostic[]>([]);
  const [selectedLevel, setSelectedLevel] = useState<(typeof LEVELS)[number]>("ALL");
  const [selectedSource, setSelectedSource] = useState("all");
  const [dateFilter, setDateFilter] = useState(defaultDateFilter);
  const [query, setQuery] = useState("");
  const [autoRefresh, setAutoRefresh] = useState(true);
  const [autoScroll, setAutoScroll] = useState(true);
  const [newestFirst, setNewestFirst] = useState(true);
  const [clearedLogKeys, setClearedLogKeys] = useState<Set<string>>(() => new Set());
  const [loading, setLoading] = useState(false);
  const [supportBundleLoading, setSupportBundleLoading] = useState(false);
  const [logFileClearLoading, setLogFileClearLoading] = useState(false);
  const [deleteDialogOpen, setDeleteDialogOpen] = useState(false);
  const [error, setError] = useState("");
  const [actionStatus, setActionStatus] = useState("");
  const [copiedLogDetailKey, setCopiedLogDetailKey] = useState("");
  const [lastUpdated, setLastUpdated] = useState<Date | null>(null);
  const [truncated, setTruncated] = useState(false);
  const logScrollRef = useRef<HTMLElement | null>(null);
  const logLoadGenerationRef = useRef(0);
  const logLoadInFlightRef = useRef(false);
  const copyResetTimerRef = useRef<number | null>(null);

  const loadLogs = useCallback(async () => {
    if (logLoadInFlightRef.current) return;
    logLoadInFlightRef.current = true;
    const loadGeneration = logLoadGenerationRef.current + 1;
    logLoadGenerationRef.current = loadGeneration;
    setLoading(true);
    setError("");
    try {
      const res = await fetchWithTimeout(
        apiUrl("/api/runtime/logs?limit=1200"),
        { credentials: "include" },
        8_000,
      );
      if (!res.ok) {
        throw new Error((await res.text()) || res.statusText);
      }
      const payload = (await res.json()) as RuntimeLogsResponse;
      if (loadGeneration !== logLoadGenerationRef.current) return;
      setLogs(payload.items || []);
      setSources(payload.sources || []);
      setTruncated(payload.truncated === true);
      void fetchWithTimeout(
        apiUrl("/api/runtime/post-processing-diagnostics?limit=8"),
        { credentials: "include" },
        5_000,
      )
        .then(async (diagnosticsRes) => {
          if (!diagnosticsRes.ok) return null;
          return (await diagnosticsRes.json()) as PostProcessingDiagnosticsResponse;
        })
        .then((diagnosticsPayload) => {
          if (loadGeneration !== logLoadGenerationRef.current || !diagnosticsPayload) return;
          setPostProcessingDiagnostics(diagnosticsPayload.items || []);
        })
        .catch((diagnosticsError) => {
          console.debug("Post-processing diagnostics refresh failed.", diagnosticsError);
        });
      setLastUpdated(new Date());
    } catch (err: any) {
      if (loadGeneration !== logLoadGenerationRef.current) return;
      setError(String(err?.message || err));
    } finally {
      logLoadInFlightRef.current = false;
      if (loadGeneration === logLoadGenerationRef.current) {
        setLoading(false);
      }
    }
  }, []);

  useEffect(() => {
    void loadLogs();
  }, [loadLogs]);

  useEffect(() => () => {
    if (copyResetTimerRef.current !== null) {
      window.clearTimeout(copyResetTimerRef.current);
    }
  }, []);

  useEffect(() => {
    if (!autoRefresh) return;
    let cancelled = false;
    let timer = 0;
    const scheduleNextRefresh = () => {
      timer = window.setTimeout(async () => {
        await loadLogs();
        if (!cancelled) scheduleNextRefresh();
      }, 2500);
    };
    scheduleNextRefresh();
    return () => {
      cancelled = true;
      window.clearTimeout(timer);
    };
  }, [autoRefresh, loadLogs]);

  const filteredLogs = useMemo(() => {
    const needle = query.trim().toLowerCase();
    return logs.filter((entry) => {
      if (clearedLogKeys.has(logEntryKey(entry))) return false;
      const level = normalizeLevel(entry.level);
      if (selectedLevel !== "ALL" && level !== selectedLevel) return false;
      if (selectedSource !== "all" && entry.source !== selectedSource) return false;
      if (dateFilter !== ALL_DATES_VALUE) {
        const dateKey = entryDateKey(entry);
        if (dateKey && dateKey !== dateFilter) return false;
        if (!dateKey && dateFilter !== defaultDateFilter) return false;
      }
      if (!needle) return true;
      return [
        entry.message,
        entry.source,
        entry.component || "",
        entry.context ? JSON.stringify(entry.context) : "",
        level,
        entry.timestamp || "",
        String(entry.timestampMs || ""),
      ]
        .join(" ")
        .toLowerCase()
        .includes(needle);
    });
  }, [clearedLogKeys, dateFilter, defaultDateFilter, logs, query, selectedLevel, selectedSource]);

  const displayedLogs = useMemo(() => {
    return filteredLogs
      .map((entry, index) => ({ entry, sortValue: entrySortValue(entry, index), index }))
      .sort((a, b) => {
        const sortDelta = newestFirst ? b.sortValue - a.sortValue : a.sortValue - b.sortValue;
        if (sortDelta !== 0) return sortDelta;
        return newestFirst ? b.index - a.index : a.index - b.index;
      })
      .map((item) => item.entry);
  }, [filteredLogs, newestFirst]);

  useEffect(() => {
    if (!autoScroll || newestFirst) return;
    const scroller = logScrollRef.current;
    if (!scroller) return;
    scroller.scrollTop = scroller.scrollHeight;
  }, [autoScroll, newestFirst, displayedLogs.length, lastUpdated]);

  const errorCount = logs.filter((entry) => ["ERROR", "CRITICAL"].includes(normalizeLevel(entry.level))).length;
  const warningCount = logs.filter((entry) => normalizeLevel(entry.level) === "WARNING").length;
  const debugCount = logs.filter((entry) => ["DEBUG", "TRACE"].includes(normalizeLevel(entry.level))).length;
  const latestPostProcessing = postProcessingDiagnostics[0] || null;
  const postProcessingFailures = postProcessingDiagnostics.filter((item) => item.status === "failure").length;
  const hasActiveFilters =
    selectedLevel !== "ALL" ||
    selectedSource !== "all" ||
    dateFilter !== defaultDateFilter ||
    query.trim().length > 0;

  const resetFilters = () => {
    setSelectedLevel("ALL");
    setSelectedSource("all");
    setDateFilter(defaultDateFilter);
    setQuery("");
  };

  const clearConsoleView = () => {
    if (!displayedLogs.length) return;
    setClearedLogKeys((previous) => {
      const next = new Set(previous);
      displayedLogs.forEach((entry) => next.add(logEntryKey(entry)));
      return next;
    });
    setActionStatus(`Cleared ${displayedLogs.length} visible log entries from this view.`);
  };

  const deleteRuntimeLogs = async () => {
    if (!logs.length && !sources.length) return;

    setDeleteDialogOpen(false);
    setLogFileClearLoading(true);
    setError("");
    setActionStatus("");
    try {
      const res = await fetchWithTimeout(apiUrl("/api/runtime/logs"), {
        method: "DELETE",
        credentials: "include",
      }, 30_000);
      const bodyText = await res.text();
      let payload: RuntimeLogsClearResponse | null = null;
      try {
        payload = bodyText ? (JSON.parse(bodyText) as RuntimeLogsClearResponse) : null;
      } catch {
        payload = null;
      }
      if (!res.ok || !payload?.ok) {
        const failureText = payload?.failures?.length
          ? payload.failures.map((failure) => `${failure.source}: ${failure.error}`).join("; ")
          : bodyText || res.statusText;
        throw new Error(failureText || res.statusText);
      }

      logLoadGenerationRef.current += 1;
      setLogs([]);
      setSources([]);
      setClearedLogKeys(new Set());
      setTruncated(false);
      setLastUpdated(new Date());
      setLoading(false);
      setActionStatus(`Cleared ${payload.cleared} runtime log source${payload.cleared === 1 ? "" : "s"}.`);
    } catch (err: any) {
      setError(`Clear logs failed: ${String(err?.message || err)}`);
    } finally {
      setDeleteDialogOpen(false);
      setLogFileClearLoading(false);
    }
  };

  const jumpToLogEdge = () => {
    const scroller = logScrollRef.current;
    if (!scroller) return;
    scroller.scrollTop = newestFirst ? 0 : scroller.scrollHeight;
  };

  const copyVisibleLogs = async () => {
    const text = displayedLogs.map(formatLogLine).join("\n");
    if (!text) return;
    try {
      await navigator.clipboard.writeText(text);
      setActionStatus(`Copied ${displayedLogs.length} visible log entries.`);
    } catch (err: any) {
      setError(`Copy failed: ${String(err?.message || err)}`);
    }
  };

  const copyLogDetail = async (text: string, key: string) => {
    if (!text) return;
    try {
      await navigator.clipboard.writeText(text);
      setCopiedLogDetailKey(key);
      if (copyResetTimerRef.current !== null) {
        window.clearTimeout(copyResetTimerRef.current);
      }
      copyResetTimerRef.current = window.setTimeout(() => {
        setCopiedLogDetailKey((current) => current === key ? "" : current);
        copyResetTimerRef.current = null;
      }, 1_600);
    } catch (err: any) {
      setError(`Copy failed: ${String(err?.message || err)}`);
    }
  };

  const downloadSupportBundle = async () => {
    setSupportBundleLoading(true);
    setError("");
    try {
      const res = await fetchWithTimeout(apiUrl("/api/runtime/support-bundle"), {
        method: "POST",
        credentials: "include",
      }, 120_000);
      if (!res.ok) {
        throw new Error((await res.text()) || res.statusText);
      }
      const blob = await res.blob();
      const filename = filenameFromDisposition(res.headers.get("Content-Disposition"));
      const url = URL.createObjectURL(blob);
      const link = document.createElement("a");
      link.href = url;
      link.download = filename;
      document.body.appendChild(link);
      link.click();
      link.remove();
      URL.revokeObjectURL(url);
      const message = `Support bundle downloaded as ${filename}. Check your Downloads folder.`;
      setActionStatus(message);
      toast({
        title: "Support bundle downloaded",
        description: `${filename} was saved by the browser download manager.`,
      });
    } catch (err: any) {
      setError(`Support bundle failed: ${String(err?.message || err)}`);
      toast({
        title: "Support bundle failed",
        description: String(err?.message || err),
        variant: "destructive",
      });
    } finally {
      setSupportBundleLoading(false);
    }
  };

  return (
    <>
      <div className="app-page-shell debug-console-page" data-page-shell="console">
        <header className="debug-console-hero">
          <div className="debug-console-intro">
            <div className="debug-console-eyebrow">
              <span className="debug-console-eyebrow-line" />
              System observability · 05
            </div>
            <div className="debug-console-title-row">
              <div>
                <h1>Debug console</h1>
                <p>Inspect runtime events, isolate failures, and package diagnostics without leaving Scriber.</p>
              </div>
            </div>
          </div>

          <div className="debug-console-overview">
            <div className="debug-console-stats" aria-label="Runtime log summary">
              <div className="debug-console-stat" aria-label={`${filteredLogs.length} of ${logs.length} logs visible`}>
                <strong>{filteredLogs.length}</strong>
                <div className="debug-console-stat-copy">
                  <span>Visible</span>
                  <small>of {logs.length} logs</small>
                </div>
                <Eye className="debug-console-stat-icon" aria-hidden="true" />
              </div>
              <div className="debug-console-stat" data-tone={errorCount ? "danger" : "quiet"} aria-label={`${errorCount} errors including critical events`}>
                <strong>{errorCount}</strong>
                <div className="debug-console-stat-copy">
                  <span>Errors</span>
                  <small>critical included</small>
                </div>
                {errorCount ? (
                  <AlertTriangle className="debug-console-stat-icon" aria-hidden="true" />
                ) : (
                  <CheckCircle2 className="debug-console-stat-icon" aria-hidden="true" />
                )}
              </div>
              <div className="debug-console-stat" data-tone={warningCount ? "warning" : "quiet"} aria-label={`${warningCount} warnings`}>
                <strong>{warningCount}</strong>
                <div className="debug-console-stat-copy">
                  <span>Warnings</span>
                  <small>needs review</small>
                </div>
                {warningCount ? (
                  <AlertTriangle className="debug-console-stat-icon" aria-hidden="true" />
                ) : (
                  <CheckCircle2 className="debug-console-stat-icon" aria-hidden="true" />
                )}
              </div>
              <div className="debug-console-stat" aria-label={`${sources.length} log sources`}>
                <strong>{sources.length}</strong>
                <div className="debug-console-stat-copy">
                  <span>Sources</span>
                  <small>{truncated ? "tail view" : "full view"}</small>
                </div>
                <Layers3 className="debug-console-stat-icon" aria-hidden="true" />
              </div>
            </div>

            <div className="debug-console-actions" aria-label="Console actions">
              <Button className="debug-console-action-button" title="Clear view" aria-label="Clear view" type="button" variant="outline" size="sm" onClick={clearConsoleView} disabled={!displayedLogs.length}>
                <Eraser className="h-4 w-4" />
                <span className="debug-console-action-label">Clear view</span>
              </Button>
              <Button
                className="debug-console-action-button"
                title="Clear logs"
                aria-label="Clear logs"
                type="button"
                variant="outline"
                size="sm"
                onClick={() => setDeleteDialogOpen(true)}
                disabled={logFileClearLoading || (!logs.length && !sources.length)}
              >
                <Trash2 className={cn("h-4 w-4", logFileClearLoading && "animate-pulse")} />
                <span className="debug-console-action-label">Clear logs</span>
              </Button>
              <Button className="debug-console-action-button" title="Copy visible logs" aria-label="Copy visible logs" type="button" variant="outline" size="sm" onClick={() => void copyVisibleLogs()} disabled={!displayedLogs.length}>
                <Clipboard className="h-4 w-4" />
                <span className="debug-console-action-label">Copy</span>
              </Button>
              <Button className="debug-console-action-button" title="Download support bundle" aria-label="Download support bundle" type="button" variant="outline" size="sm" onClick={() => void downloadSupportBundle()} disabled={supportBundleLoading}>
                <Download className={cn("h-4 w-4", supportBundleLoading && "animate-pulse")} />
                <span className="debug-console-action-label">Support</span>
              </Button>
              <Button className="debug-console-action-button debug-console-refresh-button" title="Refresh logs" aria-label="Refresh logs" type="button" size="sm" onClick={() => void loadLogs()} disabled={loading}>
                <RefreshCw className={cn("h-4 w-4", loading && "animate-spin")} />
                <span className="debug-console-action-label">Refresh</span>
              </Button>
            </div>
          </div>
        </header>

        <section className="debug-command-deck" aria-label="Log controls">
          <div className="debug-command-primary">
            <div className="debug-search-field">
              <Search className="pointer-events-none h-4 w-4" />
              <Input
                value={query}
                onChange={(event) => setQuery(event.target.value)}
                placeholder="Search messages, sources, or components"
                aria-label="Filter logs"
              />
              <span className="debug-search-hint">live filter</span>
            </div>
            <Select value={selectedSource} onValueChange={setSelectedSource}>
              <SelectTrigger className="debug-source-select" aria-label="Filter source">
                <SelectValue placeholder="Source" />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="all">All sources</SelectItem>
                {sources.map((source) => (
                  <SelectItem key={source} value={source}>{source}</SelectItem>
                ))}
              </SelectContent>
            </Select>
            <div className="debug-date-field">
              <CalendarDays className="pointer-events-none h-4 w-4" />
              <Input
                type="date"
                value={dateFilter === ALL_DATES_VALUE ? "" : dateFilter}
                onChange={(event) => setDateFilter(event.target.value || ALL_DATES_VALUE)}
                aria-label="Filter log date"
              />
            </div>
          </div>

          <div className="debug-command-secondary">
            <div className="debug-level-filter" aria-label="Filter by severity">
              <div className="debug-level-label">
                <Filter className="h-4 w-4" />
                Severity
              </div>
              <div className="debug-level-options">
                {LEVELS.map((level) => (
                  <button
                    key={level}
                    type="button"
                    aria-pressed={selectedLevel === level}
                    aria-label={level === "ALL" ? "Show all severity levels" : `Show ${level.toLowerCase()} logs`}
                    title={level === "ALL" ? "Show all severity levels" : `Show ${level.toLowerCase()} logs`}
                    data-level={level.toLowerCase()}
                    onClick={() => setSelectedLevel(level)}
                    className={cn("debug-level-button", selectedLevel === level && "is-active")}
                  >
                    <span>{level}</span>
                    {selectedLevel === level && <Check className="debug-level-selected-icon" aria-hidden="true" />}
                  </button>
                ))}
              </div>
            </div>
            <Button className="debug-reset-button" type="button" variant="ghost" onClick={resetFilters} disabled={!hasActiveFilters}>
              <Eraser className="h-4 w-4" />
              Reset
            </Button>
          </div>

          <div className="debug-runtime-controls">
            <div className="debug-toggle-group">
              <label className="debug-toggle-control">
                <span>Auto refresh</span>
                <Switch className="compact-impact-switch" checked={autoRefresh} onCheckedChange={setAutoRefresh} aria-label="Toggle auto refresh" />
              </label>
              <label className="debug-toggle-control">
                <span>Auto scroll</span>
                <Switch className="compact-impact-switch" checked={autoScroll} onCheckedChange={setAutoScroll} aria-label="Toggle auto scroll" />
              </label>
              <label className="debug-toggle-control">
                <span>Newest first</span>
                <Switch className="compact-impact-switch" checked={newestFirst} onCheckedChange={setNewestFirst} aria-label="Show newest logs first" />
              </label>
            </div>
            <Button className="debug-edge-button" type="button" variant="ghost" size="sm" onClick={jumpToLogEdge} disabled={!displayedLogs.length}>
              <ArrowDownToLine className="h-4 w-4" />
              {newestFirst ? "Jump to top" : "Jump to bottom"}
            </Button>
            <div className="debug-updated-status">
              <span className={cn("debug-live-dot", autoRefresh && "is-live")} />
              <span>{autoRefresh ? "Live" : "Paused"}</span>
              <span className="debug-updated-time">{lastUpdated ? lastUpdated.toLocaleTimeString("de-DE") : "--:--:--"}</span>
            </div>
          </div>

          {actionStatus && (
            <div className="debug-action-status" aria-live="polite" title={actionStatus}>{actionStatus}</div>
          )}
          {error && <div className="debug-error-banner">{error}</div>}
        </section>

        <div className="debug-console-workspace">
          <section className="debug-log-panel" aria-label="Runtime log stream">
            <header className="debug-log-header">
              <div>
                <span className="debug-log-kicker">Runtime stream</span>
                <h2>Live event feed</h2>
              </div>
              <div className="debug-log-meta">
                <span>{displayedLogs.length} visible</span>
                <span>{debugCount} debug</span>
                <span>{dateFilter === ALL_DATES_VALUE ? "All dates" : dateFilter}</span>
              </div>
            </header>

            <section ref={logScrollRef} className="debug-log-scroll">
              {displayedLogs.length === 0 ? (
                <div className="debug-empty-state">
                  <div className="debug-empty-mark"><Terminal className="h-5 w-5" /></div>
                  <strong>No matching events</strong>
                  <span>Adjust the active filters or refresh the runtime stream.</span>
                  {hasActiveFilters && (
                    <Button type="button" variant="outline" size="sm" onClick={resetFilters}>Reset filters</Button>
                  )}
                </div>
              ) : (
                <div className="debug-log-list">
                  {displayedLogs.map((entry, index) => {
                    const level = normalizeLevel(entry.level);
                    const Icon = iconForLevel(level);
                    return (
                      <article
                        key={`${logEntryKey(entry)}-${index}`}
                        className={cn("debug-log-row", rowStyles[level] || rowStyles.INFO)}
                        data-level={level.toLowerCase()}
                      >
                        <time className="debug-log-time" title={entry.timestamp || ""}>{formatEntryTime(entry)}</time>
                        <span className="debug-log-source" title={`${entry.source}:${entry.line}`}>{entry.source}:{entry.line}</span>
                        <span className={cn("debug-log-level", levelStyles[level] || levelStyles.INFO)}>
                          <Icon className="h-3 w-3" />
                          {level}
                        </span>
                        <div className="debug-log-message-cell">
                          {entry.component && <span className="debug-log-component">{entry.component}</span>}
                          <RuntimeLogMessage
                            message={entry.message}
                            context={entry.context}
                            copyKey={logEntryKey(entry)}
                            copiedKey={copiedLogDetailKey}
                            onCopy={(text, key) => void copyLogDetail(text, key)}
                          />
                        </div>
                      </article>
                    );
                  })}
                </div>
              )}
            </section>
          </section>

          <aside className="debug-diagnostics-panel" aria-label="Post-processing diagnostics">
            <div className="debug-diagnostics-heading">
              <div className="debug-diagnostics-mark"><Bug className="h-4 w-4" /></div>
              <div>
                <span>Pipeline</span>
                <h2>Post-processing</h2>
              </div>
              <Badge className="debug-failure-badge" variant={postProcessingFailures ? "destructive" : "outline"}>
                {postProcessingFailures} failed
              </Badge>
            </div>

            <div className="debug-diagnostic-state" data-state={latestPostProcessing?.status || "idle"}>
              <span className="debug-diagnostic-state-dot" />
              <div>
                <strong>{postProcessingStatusLabel(latestPostProcessing)}</strong>
                <span>{latestPostProcessing?.durationMs != null ? formatMs(latestPostProcessing.durationMs) : "Waiting for a run"}</span>
              </div>
            </div>

            <dl className="debug-diagnostic-list">
              <div><dt>Model</dt><dd title={latestPostProcessing?.model || ""}>{latestPostProcessing?.model || "--"}</dd></div>
              <div><dt>Input</dt><dd>{latestPostProcessing?.rawChars ?? "--"} chars</dd></div>
              <div><dt>Output</dt><dd>{latestPostProcessing?.processedChars ?? latestPostProcessing?.providerResponseChars ?? "--"} chars</dd></div>
              <div><dt>Token cap</dt><dd>{latestPostProcessing?.maxOutputTokens ?? "--"}</dd></div>
              <div><dt>Prompt</dt><dd>{latestPostProcessing?.promptChars ?? "--"} chars</dd></div>
            </dl>

            {latestPostProcessing?.fallbackToRaw ? (
              <div className="debug-fallback-note" title={latestPostProcessing.error || ""}>
                Raw fallback · {latestPostProcessing.errorType || latestPostProcessing.error || "active"}
              </div>
            ) : null}

            <div className="debug-diagnostics-foot">
              <span>Diagnostics are redacted</span>
              <span>{postProcessingDiagnostics.length} recent runs</span>
            </div>
          </aside>
        </div>
      </div>

      <AlertDialog open={deleteDialogOpen} onOpenChange={setDeleteDialogOpen}>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>Clear runtime logs?</AlertDialogTitle>
            <AlertDialogDescription>
              This clears the backend and shell logs for the debug console and for new support bundles. Existing support bundles are not changed.
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel>Cancel</AlertDialogCancel>
            <AlertDialogAction onClick={() => void deleteRuntimeLogs()} disabled={logFileClearLoading}>
              <Trash2 className="h-4 w-4" />
              Clear logs
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </>
  );
}
