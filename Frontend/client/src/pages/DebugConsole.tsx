import { useCallback, useEffect, useMemo, useState } from "react";
import { AlertTriangle, Bug, CheckCircle2, Circle, Filter, RefreshCw, Search, Terminal } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Switch } from "@/components/ui/switch";
import { apiUrl } from "@/lib/backend";
import { cn } from "@/lib/utils";
import type { RuntimeLogEntry, RuntimeLogsResponse } from "@/lib/api-types";

const LEVELS = ["ALL", "ERROR", "WARNING", "INFO", "DEBUG"] as const;

const levelStyles: Record<string, string> = {
  TRACE: "border-slate-300 bg-slate-100 text-slate-700 dark:border-slate-700 dark:bg-slate-900/40 dark:text-slate-300",
  DEBUG: "border-sky-300 bg-sky-100 text-sky-800 dark:border-sky-800 dark:bg-sky-950/45 dark:text-sky-200",
  INFO: "border-blue-300 bg-blue-100 text-blue-800 dark:border-blue-800 dark:bg-blue-950/45 dark:text-blue-200",
  SUCCESS: "border-emerald-300 bg-emerald-100 text-emerald-800 dark:border-emerald-800 dark:bg-emerald-950/45 dark:text-emerald-200",
  WARNING: "border-amber-300 bg-amber-100 text-amber-900 dark:border-amber-800 dark:bg-amber-950/50 dark:text-amber-200",
  ERROR: "border-red-300 bg-red-100 text-red-800 dark:border-red-800 dark:bg-red-950/50 dark:text-red-200",
  CRITICAL: "border-red-400 bg-red-200 text-red-950 dark:border-red-700 dark:bg-red-950 dark:text-red-100",
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
  if (value === "ERR") return "ERROR";
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
  if (entry.timestamp) {
    return entry.timestamp;
  }
  return "";
}

export default function DebugConsole() {
  const [logs, setLogs] = useState<RuntimeLogEntry[]>([]);
  const [sources, setSources] = useState<string[]>([]);
  const [selectedLevel, setSelectedLevel] = useState<(typeof LEVELS)[number]>("ALL");
  const [selectedSource, setSelectedSource] = useState("all");
  const [query, setQuery] = useState("");
  const [autoRefresh, setAutoRefresh] = useState(true);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const [lastUpdated, setLastUpdated] = useState<Date | null>(null);
  const [truncated, setTruncated] = useState(false);

  const loadLogs = useCallback(async () => {
    setLoading(true);
    setError("");
    try {
      const res = await fetch(apiUrl("/api/runtime/logs?limit=900"), { credentials: "include" });
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

  const errorCount = logs.filter((entry) => ["ERROR", "CRITICAL"].includes(normalizeLevel(entry.level))).length;
  const warningCount = logs.filter((entry) => normalizeLevel(entry.level) === "WARNING").length;

  return (
    <div className="flex h-full min-h-[calc(100vh-1.5rem)] flex-col overflow-hidden">
      <header className="border-b border-border/70 px-4 py-3 md:px-6">
        <div className="flex flex-col gap-3 lg:flex-row lg:items-center lg:justify-between">
          <div className="min-w-0">
            <div className="flex items-center gap-2">
              <Terminal className="h-5 w-5 text-foreground" />
              <h1 className="text-xl font-semibold tracking-tight text-foreground">Debug Console</h1>
            </div>
            <div className="mt-1 flex flex-wrap items-center gap-2 text-sm text-muted-foreground">
              <span>{filteredLogs.length} of {logs.length} entries</span>
              {lastUpdated && <span>Updated {lastUpdated.toLocaleTimeString("de-DE")}</span>}
              {truncated && <span>Tail view</span>}
            </div>
          </div>
          <div className="flex flex-wrap items-center gap-2">
            <Badge variant={errorCount ? "destructive" : "outline"}>{errorCount} errors</Badge>
            <Badge variant={warningCount ? "secondary" : "outline"}>{warningCount} warnings</Badge>
            <div className="flex items-center gap-2 rounded-md border border-border/70 px-3 py-1.5">
              <span className="text-sm text-muted-foreground">Auto</span>
              <Switch checked={autoRefresh} onCheckedChange={setAutoRefresh} aria-label="Toggle auto refresh" />
            </div>
            <Button type="button" variant="outline" onClick={() => void loadLogs()} disabled={loading}>
              <RefreshCw className={cn("h-4 w-4", loading && "animate-spin")} />
              Refresh
            </Button>
          </div>
        </div>
      </header>

      <section className="border-b border-border/70 px-4 py-3 md:px-6">
        <div className="grid gap-2 lg:grid-cols-[minmax(240px,1fr)_180px_220px]">
          <div className="relative">
            <Search className="pointer-events-none absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-muted-foreground" />
            <Input
              value={query}
              onChange={(event) => setQuery(event.target.value)}
              className="pl-9"
              placeholder="Filter logs..."
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
        </div>
        {error && (
          <div className="mt-3 rounded-md border border-red-300 bg-red-50 px-3 py-2 text-sm text-red-800 dark:border-red-800 dark:bg-red-950/40 dark:text-red-200">
            {error}
          </div>
        )}
      </section>

      <section className="min-h-0 flex-1 overflow-auto px-3 py-3 md:px-5">
        <div className="overflow-hidden rounded-md border border-border/70 bg-background/45">
          {filteredLogs.length === 0 ? (
            <div className="flex min-h-48 items-center justify-center text-sm text-muted-foreground">
              No matching log entries.
            </div>
          ) : (
            <div className="divide-y divide-border/60 font-mono text-[12px] leading-5">
              {filteredLogs.map((entry, index) => {
                const level = normalizeLevel(entry.level);
                const Icon = iconForLevel(level);
                return (
                  <div
                    key={`${entry.source}-${entry.line}-${index}`}
                    className={cn(
                      "grid gap-2 px-3 py-2 md:grid-cols-[76px_132px_92px_minmax(0,1fr)]",
                      level === "ERROR" || level === "CRITICAL"
                        ? "bg-red-500/5"
                        : level === "WARNING"
                          ? "bg-amber-500/7"
                          : "bg-transparent",
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
  );
}
