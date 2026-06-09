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
  Filter,
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
import { useToast } from "@/hooks/use-toast";
import { apiUrl } from "@/lib/backend";
import { cn } from "@/lib/utils";
import type { RuntimeLogEntry, RuntimeLogsClearResponse, RuntimeLogsResponse } from "@/lib/api-types";

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

export default function DebugConsole() {
  const { toast } = useToast();
  const defaultDateFilter = useMemo(() => dateInputValue(new Date()), []);
  const [logs, setLogs] = useState<RuntimeLogEntry[]>([]);
  const [sources, setSources] = useState<string[]>([]);
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
  const [lastUpdated, setLastUpdated] = useState<Date | null>(null);
  const [truncated, setTruncated] = useState(false);
  const logScrollRef = useRef<HTMLElement | null>(null);
  const logLoadGenerationRef = useRef(0);

  const loadLogs = useCallback(async () => {
    const loadGeneration = logLoadGenerationRef.current + 1;
    logLoadGenerationRef.current = loadGeneration;
    setLoading(true);
    setError("");
    try {
      const res = await fetch(apiUrl("/api/runtime/logs?limit=1200"), { credentials: "include" });
      if (!res.ok) {
        throw new Error((await res.text()) || res.statusText);
      }
      const payload = (await res.json()) as RuntimeLogsResponse;
      if (loadGeneration !== logLoadGenerationRef.current) return;
      setLogs(payload.items || []);
      setSources(payload.sources || []);
      setTruncated(payload.truncated === true);
      setLastUpdated(new Date());
    } catch (err: any) {
      if (loadGeneration !== logLoadGenerationRef.current) return;
      setError(String(err?.message || err));
    } finally {
      if (loadGeneration === logLoadGenerationRef.current) {
        setLoading(false);
      }
    }
  }, []);

  useEffect(() => {
    void loadLogs();
  }, [loadLogs]);

  useEffect(() => {
    if (!autoRefresh) return;
    const timer = window.setInterval(() => {
      void loadLogs();
    }, 2500);
    return () => window.clearInterval(timer);
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
      const res = await fetch(apiUrl("/api/runtime/logs"), {
        method: "DELETE",
        credentials: "include",
      });
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

  const downloadSupportBundle = async () => {
    setSupportBundleLoading(true);
    setError("");
    try {
      const res = await fetch(apiUrl("/api/runtime/support-bundle"), {
        method: "POST",
        credentials: "include",
      });
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
    <div className="flex h-[calc(100vh-1.5rem)] min-h-0 flex-col overflow-hidden">
      <div className="sticky top-0 z-20 shrink-0 border-b border-border/70 bg-background/95 backdrop-blur">
        <header className="border-b border-border/70 px-4 py-3 md:px-6">
          <div className="grid gap-3 xl:grid-cols-[minmax(260px,1fr)_minmax(0,auto)] xl:items-start">
            <div className="min-w-0">
              <div className="flex items-center gap-2">
                <Terminal className="h-5 w-5 text-foreground" />
                <h1 className="text-xl font-semibold tracking-tight text-foreground">Debug Console</h1>
              </div>
              <div className="mt-1 grid min-h-[3.25rem] gap-x-3 gap-y-1 text-sm text-muted-foreground sm:grid-cols-[auto_auto_auto]">
                <span className="whitespace-nowrap">{filteredLogs.length} of {logs.length} entries</span>
                <span className="whitespace-nowrap">{sources.length} sources</span>
                <span className="whitespace-nowrap">{dateFilter === ALL_DATES_VALUE ? "All dates" : `Date ${dateFilter}`}</span>
                <span className="whitespace-nowrap">
                  Updated {lastUpdated ? lastUpdated.toLocaleTimeString("de-DE") : "--:--:--"}
                </span>
                <span className="whitespace-nowrap">{truncated ? "Tail view" : "Full view"}</span>
              </div>
            </div>
            <div className="grid gap-2 xl:justify-items-end">
              <div className="grid w-full grid-cols-3 gap-2 xl:w-auto xl:min-w-[360px]">
                <Badge className="justify-center" variant={errorCount ? "destructive" : "outline"}>{errorCount} errors</Badge>
                <Badge className="justify-center" variant={warningCount ? "secondary" : "outline"}>{warningCount} warnings</Badge>
                <Badge className="justify-center" variant={debugCount ? "outline" : "secondary"}>{debugCount} debug</Badge>
              </div>
              <div className="debug-console-actions">
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
                  <span className="debug-console-action-label">Copy visible</span>
                </Button>
                <Button className="debug-console-action-button" title="Download support bundle" aria-label="Download support bundle" type="button" variant="outline" size="sm" onClick={() => void downloadSupportBundle()} disabled={supportBundleLoading}>
                  <Download className={cn("h-4 w-4", supportBundleLoading && "animate-pulse")} />
                  <span className="debug-console-action-label">Support bundle</span>
                </Button>
                <Button className="debug-console-action-button" title="Refresh logs" aria-label="Refresh logs" type="button" variant="outline" size="sm" onClick={() => void loadLogs()} disabled={loading}>
                  <RefreshCw className={cn("h-4 w-4", loading && "animate-spin")} />
                  <span className="debug-console-action-label">Refresh</span>
                </Button>
              </div>
            </div>
          </div>
        </header>

        <section className="px-4 py-3 md:px-6">
          <div className="grid gap-2 2xl:grid-cols-[minmax(220px,1fr)_180px_160px_minmax(520px,auto)_auto]">
            <div className="relative">
              <Search className="pointer-events-none absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-muted-foreground" />
              <Input
                value={query}
                onChange={(event) => setQuery(event.target.value)}
                className="pl-9"
                placeholder="Filter message, source, component..."
                aria-label="Filter logs"
              />
            </div>
            <Select value={selectedSource} onValueChange={setSelectedSource}>
              <SelectTrigger aria-label="Filter source">
                <SelectValue placeholder="Source" />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="all">All sources</SelectItem>
                {sources.map((source) => (
                  <SelectItem key={source} value={source}>{source}</SelectItem>
                ))}
              </SelectContent>
            </Select>
            <div className="relative">
              <CalendarDays className="pointer-events-none absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-muted-foreground" />
              <Input
                type="date"
                value={dateFilter === ALL_DATES_VALUE ? "" : dateFilter}
                onChange={(event) => setDateFilter(event.target.value || ALL_DATES_VALUE)}
                className="pl-9"
                aria-label="Filter log date"
              />
            </div>
            <div className="min-w-0 overflow-x-auto rounded-md border border-border/70 bg-background/35 p-1">
              <div className="flex min-w-max items-center gap-1">
                <Filter className="ml-2 h-4 w-4 shrink-0 text-muted-foreground" />
                {LEVELS.map((level) => (
                  <button
                    key={level}
                    type="button"
                    onClick={() => setSelectedLevel(level)}
                    className={cn(
                      "h-7 min-w-[70px] shrink-0 rounded px-2 text-xs font-medium transition-colors",
                      selectedLevel === level
                        ? "bg-foreground text-background"
                        : "text-muted-foreground hover:bg-muted hover:text-foreground",
                    )}
                  >
                    {level}
                  </button>
                ))}
              </div>
            </div>
            <Button className="shrink-0" type="button" variant="ghost" onClick={resetFilters} disabled={!hasActiveFilters}>
              <Eraser className="h-4 w-4" />
              Reset filters
            </Button>
          </div>

          <div className="mt-3 grid min-h-10 gap-3 text-sm text-muted-foreground xl:grid-cols-[auto_auto_auto_auto_minmax(180px,1fr)] xl:items-center">
            <label className="flex items-center justify-between gap-2 rounded-md border border-border/70 px-3 py-1.5">
              <span>Auto refresh</span>
              <Switch className="compact-impact-switch" checked={autoRefresh} onCheckedChange={setAutoRefresh} aria-label="Toggle auto refresh" />
            </label>
            <label className="flex items-center justify-between gap-2 rounded-md border border-border/70 px-3 py-1.5">
              <span>Auto scroll</span>
              <Switch className="compact-impact-switch" checked={autoScroll} onCheckedChange={setAutoScroll} aria-label="Toggle auto scroll" />
            </label>
            <label className="flex items-center justify-between gap-2 rounded-md border border-border/70 px-3 py-1.5">
              <span>Newest first</span>
              <Switch className="compact-impact-switch" checked={newestFirst} onCheckedChange={setNewestFirst} aria-label="Show newest logs first" />
            </label>
            <Button
              className="justify-self-start"
              type="button"
              variant="ghost"
              size="sm"
              onClick={jumpToLogEdge}
              disabled={!displayedLogs.length}
            >
              <ArrowDownToLine className="h-4 w-4" />
              {newestFirst ? "Top" : "Bottom"}
            </Button>
            <span className="min-h-5 min-w-0 truncate" aria-live="polite" title={actionStatus}>
              {actionStatus || "\u00a0"}
            </span>
          </div>

          {error && (
            <div className="mt-3 rounded-md border border-red-300 bg-red-50 px-3 py-2 text-sm text-red-800 dark:border-red-800 dark:bg-red-950/40 dark:text-red-200">
              {error}
            </div>
          )}
        </section>
      </div>

      <section ref={logScrollRef} className="min-h-0 flex-1 overflow-auto px-3 py-3 md:px-5">
        <div className="overflow-hidden rounded-md border border-border/70 bg-background/45">
          {displayedLogs.length === 0 ? (
            <div className="flex min-h-48 items-center justify-center text-sm text-muted-foreground">
              No matching log entries.
            </div>
          ) : (
            <div className="divide-y divide-border/60 font-mono text-[12px] leading-5">
              {displayedLogs.map((entry, index) => {
                const level = normalizeLevel(entry.level);
                const Icon = iconForLevel(level);
                return (
                  <div
                    key={`${logEntryKey(entry)}-${index}`}
                    className={cn(
                      "grid border-l-2 gap-2 px-3 py-2 md:grid-cols-[76px_160px_96px_minmax(0,1fr)]",
                      rowStyles[level] || rowStyles.INFO,
                    )}
                  >
                    <span className="text-muted-foreground">{formatEntryTime(entry)}</span>
                    <span className="truncate text-muted-foreground" title={`${entry.source}:${entry.line}`}>
                      {entry.source}:{entry.line}
                    </span>
                    <span
                      className={cn(
                        "inline-flex h-6 w-fit items-center gap-1 rounded border px-1.5 text-[11px] font-semibold",
                        levelStyles[level] || levelStyles.INFO,
                      )}
                    >
                      <Icon className="h-3 w-3" />
                      {level}
                    </span>
                    <span className="min-w-0 whitespace-pre-wrap break-words text-foreground">
                      {entry.component && (
                        <span className="mr-2 rounded bg-muted px-1.5 py-0.5 text-[11px] text-muted-foreground">
                          {entry.component}
                        </span>
                      )}
                      {entry.message}
                    </span>
                  </div>
                );
              })}
            </div>
          )}
        </div>
      </section>
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
