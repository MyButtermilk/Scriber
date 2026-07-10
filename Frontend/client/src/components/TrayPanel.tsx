"use client";

import { useCallback, useEffect, useMemo, useState, type ReactNode } from "react";
import { motion } from "motion/react";
import {
  Check,
  ChevronLeft,
  ChevronRight,
  Clock3,
  Copy,
  Download,
  FileAudio,
  Loader2,
  LogOut,
  Mic,
  MonitorUp,
  RefreshCw,
  RotateCcw,
  RotateCw,
  Settings,
  Square,
  Video,
  type LucideIcon,
} from "lucide-react";
import {
  apiUrl,
  getGlobalHotkeyStatus,
  getTrayStatus,
  hideTrayPanel,
  isTauriRuntime,
  loadBackendBaseUrlFromTauri,
  refreshGlobalHotkey,
  trayAction,
  type TrayStatus,
} from "@/lib/backend";
import type { TranscriptHistoryItem, TranscriptType } from "@/lib/api-types";
import {
  checkDesktopUpdate,
  installDesktopUpdate,
  type DesktopUpdateProgress,
} from "@/lib/desktop-updates";
import { cn } from "@/lib/utils";
import { fetchWithTimeout } from "@/lib/fetch-with-timeout";

const DEFAULT_TRAY_STATUS: TrayStatus = {
  recordingActive: false,
  recordingMode: "idle",
  updateAvailable: false,
  updateInstalling: false,
  updateMessage: "",
};
const RECENT_TRANSCRIPT_LIMIT = 8;

type TrayView = "main" | "recent";

interface TranscriptListResponse {
  items?: TranscriptHistoryItem[];
}

type TrayActionId =
  | "toggle_live"
  | "open_youtube"
  | "open_file"
  | "open_recent"
  | "show_window"
  | "open_settings"
  | "restart_app"
  | "restart_backend"
  | "quit";

function transcriptTypeLabel(type: TranscriptType | string | undefined): string {
  switch (type) {
    case "youtube":
      return "YouTube";
    case "file":
      return "File";
    case "mic":
      return "Mic";
    default:
      return "Transcript";
  }
}

function compactTranscriptTitle(item: TranscriptHistoryItem): string {
  const title = String(item.title || "").trim();
  return title || "Untitled transcript";
}

function compactTranscriptDetail(item: TranscriptHistoryItem): string {
  return [transcriptTypeLabel(item.type), item.date, item.duration]
    .map((value) => String(value || "").trim())
    .filter(Boolean)
    .join(" • ");
}

function statusLabel(status: TrayStatus): string {
  if (status.recordingActive) return "Recording";
  if (status.updateInstalling) return "Installing update";
  if (status.updateAvailable) return "Update ready";
  return "Ready";
}

function StatusIndicator({ status }: { status: TrayStatus }) {
  if (status.recordingActive) {
    return (
      <span className="flex h-4 w-4 items-center justify-center rounded-full bg-red-500 text-white shadow-[0_0_0_4px_rgba(239,68,68,0.12)]">
        <Square className="h-2 w-2 fill-current" aria-hidden="true" />
      </span>
    );
  }

  if (status.updateInstalling) {
    return (
      <span className="flex h-4 w-4 items-center justify-center rounded-full bg-blue-600 text-white shadow-[0_0_0_4px_rgba(37,99,235,0.12)]">
        <Loader2 className="h-2.5 w-2.5 animate-spin" aria-hidden="true" />
      </span>
    );
  }

  if (status.updateAvailable) {
    return (
      <span className="flex h-4 w-4 items-center justify-center rounded-full bg-blue-600 text-white shadow-[0_0_0_4px_rgba(37,99,235,0.12)]">
        <Download className="h-2.5 w-2.5" strokeWidth={2.4} aria-hidden="true" />
      </span>
    );
  }

  return <span className="h-2 w-2 rounded-full bg-emerald-500 shadow-[0_0_0_4px_rgba(16,185,129,0.12)]" />;
}

function TrayRow({
  icon: Icon,
  label,
  detail,
  shortcut,
  trailing,
  onClick,
  variant = "default",
  disabled = false,
}: {
  icon: LucideIcon;
  label: string;
  detail?: string;
  shortcut?: string;
  trailing?: ReactNode;
  onClick: () => void;
  variant?: "default" | "primary" | "danger" | "update";
  disabled?: boolean;
}) {
  return (
    <motion.button
      type="button"
      whileTap={disabled ? undefined : { scale: 0.985 }}
      onClick={disabled ? undefined : onClick}
      disabled={disabled}
      className={cn(
        "group flex h-[42px] w-full items-center gap-3 rounded-[12px] px-3 text-left outline-none transition-colors duration-150",
        "focus-visible:ring-2 focus-visible:ring-blue-500/60 focus-visible:ring-offset-2 focus-visible:ring-offset-white/80",
        variant === "default" && "text-slate-950 hover:bg-slate-950/[0.055]",
        variant === "primary" && "bg-blue-50 text-blue-700 hover:bg-blue-100",
        variant === "danger" && "bg-red-50 text-red-700 hover:bg-red-100",
        variant === "update" && "bg-blue-600 text-white shadow-[0_10px_26px_-18px_rgba(37,99,235,0.85)] hover:bg-blue-500",
        disabled && variant !== "update" && "cursor-default opacity-55",
        disabled && variant === "update" && "cursor-default",
      )}
    >
      <span
        className={cn(
          "flex h-7 w-7 shrink-0 items-center justify-center rounded-[9px]",
          variant === "default" && "text-slate-900",
          variant === "primary" && "bg-white/70 text-blue-700",
          variant === "danger" && "bg-white/80 text-red-700",
          variant === "update" && "bg-white/16 text-white",
        )}
      >
        <Icon
          className={cn(
            "h-[18px] w-[18px]",
            variant === "danger" && Icon === Square && "fill-current",
            Icon === Loader2 && "animate-spin",
          )}
        />
      </span>
      <span className="min-w-0 flex-1">
        <span className="block truncate text-[14px] font-semibold leading-[18px] tracking-normal">{label}</span>
        {detail ? (
          <span
            className={cn(
              "mt-px block truncate text-[11px] leading-[13px]",
              variant === "update" ? "text-white/78" : "text-slate-500",
            )}
          >
            {detail}
          </span>
        ) : null}
      </span>
      {shortcut ? (
        <span
          className={cn(
            "ml-2 shrink-0 text-[12px] font-semibold leading-none",
            variant === "primary" ? "text-blue-700/78" : "text-slate-400",
          )}
        >
          {shortcut}
        </span>
      ) : null}
      {trailing ? <span className="ml-2 shrink-0 text-slate-400">{trailing}</span> : null}
    </motion.button>
  );
}

function RecentTranscriptRow({
  item,
  copied,
  onCopy,
}: {
  item: TranscriptHistoryItem;
  copied: boolean;
  onCopy: () => void;
}) {
  return (
    <motion.button
      type="button"
      whileTap={{ scale: 0.985 }}
      onClick={onCopy}
      className={cn(
        "group flex h-[46px] w-full items-center gap-3 rounded-[12px] px-3 text-left outline-none transition-colors duration-150",
        "text-slate-950 hover:bg-slate-950/[0.055]",
        "focus-visible:ring-2 focus-visible:ring-blue-500/60 focus-visible:ring-offset-2 focus-visible:ring-offset-white/80",
        copied && "bg-emerald-50 text-emerald-700",
      )}
    >
      <span
        className={cn(
          "flex h-7 w-7 shrink-0 items-center justify-center rounded-[9px]",
          copied ? "bg-white/80 text-emerald-700" : "text-slate-900",
        )}
      >
        {copied ? <Check className="h-[18px] w-[18px]" /> : <Copy className="h-[18px] w-[18px]" />}
      </span>
      <span className="min-w-0 flex-1">
        <span className="block truncate text-[13px] font-semibold leading-[17px] tracking-normal">
          {compactTranscriptTitle(item)}
        </span>
        <span className={cn("mt-px block truncate text-[11px] leading-[13px]", copied ? "text-emerald-600" : "text-slate-500")}>
          {copied ? "Copied to clipboard" : compactTranscriptDetail(item)}
        </span>
      </span>
    </motion.button>
  );
}

function formatShortcut(raw: string | undefined): string {
  const value = String(raw || "").trim();
  if (!value) return "";
  return value
    .split("+")
    .map((part) => {
      const token = part.trim().toLowerCase();
      if (!token) return "";
      if (token === "ctrl" || token === "control") return "Ctrl";
      if (token === "alt" || token === "option") return "Alt";
      if (token === "shift") return "Shift";
      if (token === "cmd" || token === "command" || token === "meta" || token === "super") return "Win";
      if (token.length === 1) return token.toUpperCase();
      return token.charAt(0).toUpperCase() + token.slice(1);
    })
    .filter(Boolean)
    .join("+");
}

export default function TrayPanel() {
  const [backendReady, setBackendReady] = useState(!isTauriRuntime());
  const [view, setView] = useState<TrayView>("main");
  const [status, setStatus] = useState<TrayStatus>(DEFAULT_TRAY_STATUS);
  const [recordingShortcut, setRecordingShortcut] = useState("");
  const [installing, setInstalling] = useState(false);
  const [checkingUpdates, setCheckingUpdates] = useState(false);
  const [updateCheckMessage, setUpdateCheckMessage] = useState("");
  const [progress, setProgress] = useState<DesktopUpdateProgress | null>(null);
  const [error, setError] = useState("");
  const [recentItems, setRecentItems] = useState<TranscriptHistoryItem[]>([]);
  const [recentLoaded, setRecentLoaded] = useState(false);
  const [recentLoading, setRecentLoading] = useState(false);
  const [recentError, setRecentError] = useState("");
  const [copiedTranscriptId, setCopiedTranscriptId] = useState("");

  useEffect(() => {
    document.documentElement.dataset.scriberTrayWindow = "true";
    document.body.dataset.scriberTrayWindow = "true";
    return () => {
      delete document.documentElement.dataset.scriberTrayWindow;
      delete document.body.dataset.scriberTrayWindow;
    };
  }, []);

  useEffect(() => {
    let cancelled = false;
    void loadBackendBaseUrlFromTauri().finally(() => {
      if (!cancelled) {
        setBackendReady(true);
      }
    });
    return () => {
      cancelled = true;
    };
  }, []);

  useEffect(() => {
    if (!isTauriRuntime()) return;
    let unlisten: (() => void) | undefined;
    let disposed = false;
    void getTrayStatus()
      .then((value) => {
        if (value) setStatus(value);
      })
      .catch((error) => console.debug("Tray status lookup failed.", error));
    void import("@tauri-apps/api/event")
      .then(({ listen }) =>
        listen<TrayStatus>("scriber-tray-status", (event) => {
          setStatus({ ...DEFAULT_TRAY_STATUS, ...event.payload });
        }),
      )
      .then((cleanup) => {
        if (disposed) {
          cleanup();
        } else {
          unlisten = cleanup;
        }
      })
      .catch((error) => console.debug("Tray status listener failed.", error));
    return () => {
      disposed = true;
      unlisten?.();
    };
  }, []);

  useEffect(() => {
    if (!isTauriRuntime() || !backendReady) return;
    let cancelled = false;
    const applyShortcut = (hotkey: string | undefined) => {
      if (!cancelled) {
        setRecordingShortcut(formatShortcut(hotkey));
      }
    };
    void refreshGlobalHotkey()
      .then((value) => {
        if (value?.hotkey) {
          applyShortcut(value.hotkey);
          return;
        }
        return getGlobalHotkeyStatus().then((fallback) => applyShortcut(fallback?.hotkey));
      })
      .catch((error) => console.debug("Tray hotkey lookup failed.", error));
    return () => {
      cancelled = true;
    };
  }, [backendReady]);

  useEffect(() => {
    if (!isTauriRuntime()) return;
    const handleKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        void hideTrayPanel();
      }
    };
    const handleBlur = () => {
      window.setTimeout(() => void hideTrayPanel(), 140);
    };
    window.addEventListener("keydown", handleKeyDown);
    window.addEventListener("blur", handleBlur);
    return () => {
      window.removeEventListener("keydown", handleKeyDown);
      window.removeEventListener("blur", handleBlur);
    };
  }, []);

  const runAction = useCallback(async (action: TrayActionId) => {
    setError("");
    try {
      await trayAction(action);
      if (action === "toggle_live") {
        window.setTimeout(() => void hideTrayPanel(), 120);
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err || "Tray action failed."));
    }
  }, []);

  const loadRecentTranscripts = useCallback(async () => {
    if (!backendReady) return;
    setRecentLoading(true);
    setRecentError("");
    try {
      const response = await fetchWithTimeout(apiUrl("/api/transcripts?limit=20&offset=0"), {
        credentials: "include",
        cache: "no-store",
      }, 10_000);
      if (!response.ok) {
        throw new Error(`Could not load recent transcripts (${response.status}).`);
      }
      const payload = (await response.json()) as TranscriptListResponse;
      const items = Array.isArray(payload.items) ? payload.items : [];
      setRecentItems(
        items
          .filter((item) => item.status === "completed" && String(item.id || "").trim())
          .slice(0, RECENT_TRANSCRIPT_LIMIT),
      );
      setRecentLoaded(true);
    } catch (err) {
      setRecentError(err instanceof Error ? err.message : String(err || "Could not load recent transcripts."));
    } finally {
      setRecentLoading(false);
    }
  }, [backendReady]);

  const openRecentView = useCallback(() => {
    setError("");
    setRecentError("");
    setCopiedTranscriptId("");
    setView("recent");
    if (!recentLoaded && !recentLoading) {
      void loadRecentTranscripts();
    }
  }, [loadRecentTranscripts, recentLoaded, recentLoading]);

  const copyRecentTranscript = useCallback(async (item: TranscriptHistoryItem) => {
    const transcriptId = String(item.id || "").trim();
    if (!transcriptId) return;
    setRecentError("");
    setCopiedTranscriptId("");
    try {
      await trayAction(`copy_transcript:${transcriptId}`);
      setCopiedTranscriptId(transcriptId);
      window.setTimeout(() => void hideTrayPanel(), 650);
    } catch (err) {
      setRecentError(err instanceof Error ? err.message : String(err || "Could not copy transcript."));
    }
  }, []);

  const installUpdate = useCallback(async () => {
    if (installing || status.updateInstalling || !status.updateAvailable) {
      return;
    }
    setInstalling(true);
    setProgress(null);
    setError("");
    try {
      await installDesktopUpdate(setProgress);
    } catch (err) {
      setInstalling(false);
      setError(err instanceof Error ? err.message : String(err || "Update installation failed."));
    }
  }, [installing, status.updateAvailable, status.updateInstalling]);

  const checkForUpdates = useCallback(async () => {
    if (checkingUpdates || installing) {
      return;
    }
    setCheckingUpdates(true);
    setUpdateCheckMessage("");
    setError("");
    try {
      const nextStatus = await checkDesktopUpdate();
      setUpdateCheckMessage(nextStatus.message || "Update check finished.");
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err || "Update check failed."));
    } finally {
      setCheckingUpdates(false);
    }
  }, [checkingUpdates, installing]);

  const updateDetail = useMemo(() => {
    if (installing || status.updateInstalling) {
      if (progress?.percent != null) {
        return `${progress.percent}% downloaded`;
      }
      return progress?.message || "Preparing installer";
    }
    if (status.updateVersion) {
      return `Scriber ${status.updateVersion}`;
    }
    return "Install and restart";
  }, [installing, progress, status.updateInstalling, status.updateVersion]);

  const updateCheckDetail = useMemo(() => {
    if (checkingUpdates) return "Checking GitHub";
    if (updateCheckMessage) return updateCheckMessage;
    if (status.updateAvailable) return "Check GitHub again";
    return "Manual update check";
  }, [checkingUpdates, status.updateAvailable, status.updateVersion, updateCheckMessage]);

  const showUpdateInstallBanner = status.updateAvailable || status.updateInstalling || installing;
  const updateInstallTitle = (() => {
    if (installing || status.updateInstalling) return "Installing update";
    if (status.updateVersion) return `Install Scriber ${status.updateVersion}`;
    return "Install update";
  })();
  const updateInstallDetail = (() => {
    if (installing || status.updateInstalling) return updateDetail;
    return "Download, install, and restart Scriber.";
  })();

  return (
    <main className="flex h-screen w-screen items-center justify-center bg-transparent p-2 text-slate-950 antialiased">
      <motion.section
        initial={{ opacity: 0, y: 10, scale: 0.985 }}
        animate={{ opacity: 1, y: 0, scale: 1 }}
        transition={{ duration: 0.18, ease: [0.16, 1, 0.3, 1] }}
        className="flex h-full w-full flex-col overflow-hidden rounded-[24px] border border-white/70 bg-[rgba(248,250,252,0.96)] p-4 shadow-[0_26px_70px_-34px_rgba(15,23,42,0.78),0_8px_24px_-20px_rgba(15,23,42,0.45)] backdrop-blur-2xl"
      >
        <header className="flex items-center gap-3 pb-3">
          <div className="flex h-12 w-12 shrink-0 items-center justify-center rounded-full border border-slate-200/80 bg-white shadow-[0_12px_28px_-22px_rgba(15,23,42,0.75)]">
            <img src="/favicon.svg" alt="" className="h-8 w-8 object-contain" draggable={false} />
          </div>
          <div className="min-w-0 flex-1">
            <h1 className="truncate text-[21px] font-semibold leading-6 tracking-normal text-slate-950">Scriber</h1>
            <div className="mt-0.5 flex items-center gap-2 text-[11px] font-medium text-slate-500">
              <StatusIndicator status={status} />
              <span className="truncate">{statusLabel(status)}</span>
            </div>
          </div>
        </header>

        <div className="h-px bg-slate-200/80" />

        {view === "main" && showUpdateInstallBanner ? (
          <div className="pt-2.5">
            <motion.button
              type="button"
              whileTap={installing || status.updateInstalling ? undefined : { scale: 0.985 }}
              onClick={installUpdate}
              disabled={installing || status.updateInstalling || !status.updateAvailable}
              className={cn(
                "flex h-[50px] w-full items-center gap-3 rounded-[14px] bg-blue-600 px-3 text-left text-white outline-none transition-colors",
                "shadow-[0_16px_30px_-20px_rgba(37,99,235,0.95)] hover:bg-blue-500",
                "focus-visible:ring-2 focus-visible:ring-blue-500/60 focus-visible:ring-offset-2 focus-visible:ring-offset-white/80",
                (installing || status.updateInstalling) && "cursor-default hover:bg-blue-600",
              )}
            >
              <span className="flex h-8 w-8 shrink-0 items-center justify-center rounded-[10px] bg-white/16">
                {installing || status.updateInstalling ? (
                  <Loader2 className="h-[18px] w-[18px] animate-spin" aria-hidden="true" />
                ) : (
                  <Download className="h-[18px] w-[18px]" strokeWidth={2.35} aria-hidden="true" />
                )}
              </span>
              <span className="min-w-0 flex-1">
                <span className="block truncate text-[14px] font-semibold leading-[18px]">
                  {updateInstallTitle}
                </span>
                <span className="mt-px block truncate text-[11px] font-medium leading-[13px] text-white/78">
                  {updateInstallDetail}
                </span>
              </span>
            </motion.button>
          </div>
        ) : null}

        <div className="min-h-0 flex-1 overflow-hidden py-2.5">
          {view === "main" ? (
            <div className="flex flex-col gap-1.5">
              <TrayRow
                icon={status.recordingActive ? Square : Mic}
                label={status.recordingActive ? "Stop Recording" : "Start Live Transcription"}
                detail={status.recordingActive ? "Live microphone is active" : "Use the configured microphone"}
                shortcut={recordingShortcut}
                variant={status.recordingActive ? "danger" : "primary"}
                disabled={!backendReady}
                onClick={() => void runAction("toggle_live")}
              />
              <TrayRow
                icon={Video}
                label="YouTube Transcription"
                onClick={() => void runAction("open_youtube")}
              />
              <TrayRow
                icon={FileAudio}
                label="Transcribe File"
                onClick={() => void runAction("open_file")}
              />

              <div className="my-0.5 h-px bg-slate-200/80" />

              <TrayRow
                icon={Clock3}
                label="Recent Transcripts"
                detail="Select one to copy"
                trailing={<ChevronRight className="h-4 w-4" />}
                disabled={!backendReady}
                onClick={openRecentView}
              />
              <TrayRow
                icon={MonitorUp}
                label="Open Main Window"
                onClick={() => void runAction("show_window")}
              />

              <div className="my-0.5 h-px bg-slate-200/80" />

              <TrayRow
                icon={Settings}
                label="Settings"
                onClick={() => void runAction("open_settings")}
              />
              <TrayRow
                icon={checkingUpdates ? Loader2 : RefreshCw}
                label={status.updateAvailable ? "Check Again" : "Check for Updates"}
                detail={updateCheckDetail}
                disabled={checkingUpdates || installing}
                onClick={() => void checkForUpdates()}
              />
              <TrayRow
                icon={RotateCcw}
                label="Restart Backend"
                detail="Only restart the local worker"
                onClick={() => void runAction("restart_backend")}
              />
            </div>
          ) : (
            <div className="flex h-full flex-col gap-1.5">
              <div className="flex items-center gap-2 px-1 pb-1">
                <button
                  type="button"
                  className="flex h-8 w-8 shrink-0 items-center justify-center rounded-[10px] text-slate-700 outline-none transition-colors hover:bg-slate-950/[0.055] focus-visible:ring-2 focus-visible:ring-blue-500/60"
                  onClick={() => {
                    setView("main");
                    setRecentError("");
                  }}
                  aria-label="Back to tray menu"
                >
                  <ChevronLeft className="h-5 w-5" />
                </button>
                <div className="min-w-0 flex-1">
                  <div className="truncate text-[14px] font-semibold leading-[18px] text-slate-950">
                    Recent Transcripts
                  </div>
                  <div className="truncate text-[11px] font-medium leading-[13px] text-slate-500">
                    Select one to copy
                  </div>
                </div>
                <button
                  type="button"
                  className="flex h-8 w-8 shrink-0 items-center justify-center rounded-[10px] text-slate-700 outline-none transition-colors hover:bg-slate-950/[0.055] focus-visible:ring-2 focus-visible:ring-blue-500/60 disabled:opacity-50"
                  onClick={() => void loadRecentTranscripts()}
                  disabled={recentLoading || !backendReady}
                  aria-label="Refresh recent transcripts"
                >
                  <RefreshCw className={cn("h-4 w-4", recentLoading && "animate-spin")} />
                </button>
              </div>

              <div className="h-px bg-slate-200/80" />

              {recentLoading ? (
                <TrayRow
                  icon={Loader2}
                  label="Loading transcripts"
                  detail="Checking recent history"
                  disabled
                  onClick={() => undefined}
                />
              ) : recentItems.length > 0 ? (
                recentItems.map((item) => (
                  <RecentTranscriptRow
                    key={item.id}
                    item={item}
                    copied={copiedTranscriptId === item.id}
                    onCopy={() => void copyRecentTranscript(item)}
                  />
                ))
              ) : (
                <TrayRow
                  icon={Clock3}
                  label="No completed transcripts"
                  detail={recentLoaded ? "Nothing available to copy" : "Refresh recent transcripts"}
                  disabled
                  onClick={() => undefined}
                />
              )}
            </div>
          )}
        </div>

        {view === "main" ? (
          <div className="border-t border-slate-200/80 pt-2.5">
            <div className="flex flex-col gap-1.5">
              <TrayRow
                icon={RotateCw}
                label="Restart Application"
                onClick={() => void runAction("restart_app")}
              />
              <TrayRow icon={LogOut} label="Quit Application" onClick={() => void runAction("quit")} />
            </div>
          </div>
        ) : null}

        {error || recentError ? (
          <div className="rounded-[14px] border border-red-200 bg-red-50 px-3 py-2 text-[12px] font-medium leading-4 text-red-700">
            {error || recentError}
          </div>
        ) : null}
      </motion.section>
    </main>
  );
}
