import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  AlertTriangle,
  ArrowDownToLine,
  Bug,
  CheckCircle2,
  Circle,
  Clipboard,
  Download,
  Eraser,
  Filter,
  RefreshCw,
  Search,
  Terminal,
} from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Switch } from "@/components/ui/switch";
import { apiUrl } from "@/lib/backend";
import { cn } from "@/lib/utils";
import type { RuntimeLogEntry, RuntimeLogsResponse } from "@/lib/api-types";

const LEVELS = ["ALL", "CRITICAL", "ERROR", "WARNING", "INFO", "DEBUG", "TRACE"] as const;

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

function filenameFromDisposition(disposition: string | null) {
  const match = disposition?.match(/filename="?([^";]+)"?/i);
  return match?.[1] || `scriber-support-bundle-${Date.now()}.zip`;
}

export default function DebugConsole() {
  const [logs, setLogs] = useState<RuntimeLogEntry[]>([]);
  const [sources, setSources] = useState<string[]>([]);
  const [selectedLevel, setSelectedLevel] = useState<(typeof LEVELS)[number]>("ALL");
  const [selectedSource, setSelectedSource] = useState("all");
  const [query, setQuery] = useState("");
  const [autoRefresh, setAutoRefresh] = useState(true);
  const [autoScroll, setAutoScroll] = useState(true);
  const [newestFirst, setNewestFirst] = useState(false);
  const [loading, setLoading] = useState(false);
  const [supportBundleLoading, setSupportBundleLoading] = useState(false);
  const [error, setError] = useState("");
  const [actionStatus, setActionStatus] = useState("");
  const [lastUpdated, setLastUpdated] = useState<Date | null>(null);
  const [truncated, setTruncated] = useState(false);
  const logEndRef = useRef<HTMLDivElement | null>(null);

  const loadLogs = useCallback(async () => {
    setLoading(true);
    setError("");
    try {
      const res = await fetch(apiUrl("/api/runtime/logs?limit=1200"), { credentials: "include" });
      if (!res.ok) {
        throw new Error((await res.text()) || res.statusText);
      }
      const payload = (await res.json()) as RuntimeLogsResponse;
      setLogs(payload.items || []);
      setSources(payload.sources || []);
      setTruncated(payload.truncated === true);
      setLastUpdated(new Date());
    } catch (err: any) {
      setError(String(err?.message || err));
    } finally {
      setLoading(false);
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
      const level = normalizeLevel(entry.level);
      if (selectedLevel !== "ALL" && level !== selectedLevel) return false;
      if (selectedSource !== "all" && entry.source !== selectedSource) return false;
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
  }, [logs, query, selectedLevel, selectedSource]);

  const displayedLogs = useMemo(
    () => (newestFirst ? [...filteredLogs].reverse() : filteredLogs),
    [filteredLogs, newestFirst],
  );

  useEffect(() => {
    if (!autoScroll || newestFirst) return;
    logEndRef.current?.scrollIntoView({ block: "end" });
  }, [autoScroll, newestFirst, displayedLogs.length, lastUpdated]);

  const errorCount = logs.filter((entry) => ["ERROR", "CRITICAL"].includes(normalizeLevel(entry.level))).length;
  const warningCount = logs.filter((entry) => normalizeLevel(entry.level) === "WARNING").length;
  const debugCount = logs.filter((entry) => ["DEBUG", "TRACE"].includes(normalizeLevel(entry.level))).length;
  const hasActiveFilters = selectedLevel !== "ALL" || selectedSource !== "all" || query.trim().length > 0;

  const resetFilters = () => {
    setSelectedLevel("ALL");
    setSelectedSource("all");
    setQuery("");
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
      const url = URL.createObjectURL(blob);
      const link = document.createElement("a");
      link.href = url;
      link.download = filenameFromDisposition(res.headers.get("Content-Disposition"));
      document.body.appendChild(link);
      link.click();
      link.remove();
      URL.revokeObjectURL(url);
      setActionStatus("Support bundle downloaded.");
    } catch (err: any) {
      setError(`Support bundle failed: ${String(err?.message || err)}`);
    } finally {
      setSupportBundleLoading(false);
    }
  };

  return (
    <div className="flex h-full min-h-[calc(100vh-1.5rem)] flex-col overflow-hidden">
      <header className="border-b border-border/70 px-4 py-3 md:px-6">
        <div className="flex flex-col gap-3 xl:flex-row xl:items-center xl:justify-between">
          <div className="min-w-0">
            <div className="flex items-center gap-2">
              <Terminal className="h-5 w-5 text-foreground" />
              <h1 className="text-xl font-semibold tracking-tight text-foreground">Debug Console</h1>
            </div>
            <div className="mt-1 flex flex-wrap items-center gap-2 text-sm text-muted-foreground">
              <span>{filteredLogs.length} of {logs.length} entries</span>
              <span>{sources.length} sources</span>
              {lastUpdated && <span>Updated {lastUpdated.toLocaleTimeString("de-DE")}</span>}
              {truncated && <span>Tail view</span>}
            </div>
          </div>
          <div className="flex flex-wrap items-center gap-2">
            <Badge variant={errorCount ? "destructive" : "outline"}>{errorCount} errors</Badge>
            <Badge variant={warningCount ? "secondary" : "outline"}>{warningCount} warnings</Badge>
            <Badge variant={debugCount ? "outline" : "secondary"}>{debugCount} debug</Badge>
            <Button type="button" variant="outline" onClick={() => void copyVisibleLogs()} disabled={!displayedLogs.length}>
              <Clipboard className="h-4 w-4" />
              Copy visible
            </Button>
            <Button type="button" variant="outline" onClick={() => void downloadSupportBundle()} disabled={supportBundleLoading}>
              <Download className={cn("h-4 w-4", supportBundleLoading && "animate-pulse")} />
              Support bundle
            </Button>
            <Button type="button" variant="outline" onClick={() => void loadLogs()} disabled={loading}>
              <RefreshCw className={cn("h-4 w-4", loading && "animate-spin")} />
              Refresh
            </Button>
          </div>
        </div>
      </header>

      <section className="border-b border-border/70 px-4 py-3 md:px-6">
        <div className="grid gap-2 xl:grid-cols-[minmax(260px,1fr)_190px_360px_auto]">
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
          <div className="flex items-center gap-1 rounded-md border border-border/70 bg-background/35 p-1">
            <Filter className="ml-2 h-4 w-4 shrink-0 text-muted-foreground" />
            {LEVELS.map((level) => (
              <button
                key={level}
                type="button"
                onClick={() => setSelectedLevel(level)}
                className={cn(
                  "h-7 min-w-0 flex-1 rounded px-2 text-xs font-medium transition-colors",
                  selectedLevel === level
                    ? "bg-foreground text-background"
                    : "text-muted-foreground hover:bg-muted hover:text-foreground",
                )}
              >
                {level}
              </button>
            ))}
          </div>
          <Button type="button" variant="ghost" onClick={resetFilters} disabled={!hasActiveFilters}>
            <Eraser className="h-4 w-4" />
            Clear
          </Button>
        </div>

        <div className="mt-3 flex flex-wrap items-center gap-3 text-sm text-muted-foreground">
          <label className="flex items-center gap-2 rounded-md border border-border/70 px-3 py-1.5">
            <span>Auto refresh</span>
            <Switch checked={autoRefresh} onCheckedChange={setAutoRefresh} aria-label="Toggle auto refresh" />
          </label>
          <label className="flex items-center gap-2 rounded-md border border-border/70 px-3 py-1.5">
            <span>Auto scroll</span>
            <Switch checked={autoScroll} onCheckedChange={setAutoScroll} aria-label="Toggle auto scroll" />
          </label>
          <label className="flex items-center gap-2 rounded-md border border-border/70 px-3 py-1.5">
            <span>Newest first</span>
            <Switch checked={newestFirst} onCheckedChange={setNewestFirst} aria-label="Show newest logs first" />
          </label>
          <Button
            type="button"
            variant="ghost"
            size="sm"
            onClick={() => logEndRef.current?.scrollIntoView({ block: "end" })}
            disabled={!displayedLogs.length || newestFirst}
          >
            <ArrowDownToLine className="h-4 w-4" />
            Bottom
          </Button>
          {actionStatus && <span>{actionStatus}</span>}
        </div>

        {error && (
          <div className="mt-3 rounded-md border border-red-300 bg-red-50 px-3 py-2 text-sm text-red-800 dark:border-red-800 dark:bg-red-950/40 dark:text-red-200">
            {error}
          </div>
        )}
      </section>

      <section className="min-h-0 flex-1 overflow-auto px-3 py-3 md:px-5">
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
                    key={`${entry.source}-${entry.line}-${index}`}
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
              <div ref={logEndRef} />
            </div>
          )}
        </div>
      </section>
    </div>
  );
}
