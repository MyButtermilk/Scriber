"use client";

import { useCallback, useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { motion } from "motion/react";
import { listen } from "@tauri-apps/api/event";
import {
  CalendarClock,
  Check,
  ChevronLeft,
  ChevronRight,
  Clock3,
  Copy,
  Download,
  FileAudio,
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
  refreshGlobalHotkey,
  trayAction,
  type TrayStatus,
} from "@/lib/backend";
import type { TrayTranscriptItem, TranscriptType } from "@/lib/api-types";
import { checkDesktopUpdate, installDesktopUpdate, type DesktopUpdateProgress } from "@/lib/desktop-updates";
import { isInitialBackendAccessReady, loadInitialBackendAccess } from "@/lib/initial-backend-access";
import { cn } from "@/lib/utils";
import { fetchWithTimeout } from "@/lib/fetch-with-timeout";
import { useI18n, type TranslationValues } from "@/i18n";
import { WavePhysicsLoader } from "@/components/ui/wave-physics-loader";

const DEFAULT_TRAY_STATUS: TrayStatus = {
  recordingActive: false,
  recordingMode: "idle",
  updateAvailable: false,
  updateInstalling: false,
  updateMessage: "",
};
const RECENT_TRANSCRIPT_LIMIT = 8;
const TRANSCRIPT_PREVIEW_CONTROLS = new RegExp("[\\p{Cc}\\p{Cf}]", "gu");

type TrayView = "main" | "recent";

interface TranscriptListResponse {
  items?: TrayTranscriptItem[];
}

interface TrayLocalizedMessage {
  source: string;
  values?: TranslationValues;
}

type Translate = (source: string, values?: TranslationValues) => string;
type FormatDate = (value: Date | number | string, options?: Intl.DateTimeFormatOptions) => string;
type FormatLegacyDate = (value: string) => string;

type TrayActionId =
  | "toggle_live"
  | "open_meetings"
  | "open_youtube"
  | "open_file"
  | "open_recent"
  | "show_window"
  | "open_settings"
  | "restart_app"
  | "restart_backend"
  | "quit";

function transcriptTypeLabel(type: TranscriptType | string | undefined, t: Translate): string {
  switch (type) {
    case "youtube":
      return "YouTube";
    case "file":
      return t("File");
    case "mic":
      return t("Mic");
    default:
      return t("Transcript");
  }
}

function compactTranscriptTitle(item: TrayTranscriptItem, t: Translate): string {
  if (item.contentUnavailable) return t("Transcript unavailable. Refresh recent transcripts.");
  // Render only a bounded plain-text label. React escapes markup and ampersands;
  // stripping controls also prevents bidi overrides and embedded menu shortcuts.
  const preview = String(item.preview || "")
    .normalize("NFKC")
    .replace(/\s+/g, " ")
    .replace(TRANSCRIPT_PREVIEW_CONTROLS, "")
    .trim();
  const chars = Array.from(preview);
  return (chars.length > 72 ? chars.slice(0, 71).join("") + "…" : preview) || t("Empty transcript");
}

function compactTranscriptDetail(
  item: TrayTranscriptItem,
  t: Translate,
  formatDate: FormatDate,
  formatLegacyDate: FormatLegacyDate,
): string {
  const localizedDate = item.createdAt
    ? formatDate(item.createdAt, { dateStyle: "medium", timeStyle: "short" })
    : formatLegacyDate(String(item.date || ""));
  return [transcriptTypeLabel(item.type, t), localizedDate, item.duration]
    .map((value) => String(value || "").trim())
    .filter(Boolean)
    .join(" • ");
}

function statusLabel(status: TrayStatus, t: Translate): string {
  if (status.recordingActive) return t("Recording");
  return t("Ready");
}

function StatusIndicator({ status }: { status: TrayStatus }) {
  if (status.recordingActive) {
    return (
      <span className="flex h-4 w-4 items-center justify-center rounded-full bg-red-500 text-white shadow-[0_0_0_4px_rgba(239,68,68,0.12)]">
        <Square className="h-2 w-2 fill-current" aria-hidden="true" />
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
  loading = false,
}: {
  icon: LucideIcon;
  label: string;
  detail?: string;
  shortcut?: string;
  trailing?: ReactNode;
  onClick: () => void;
  variant?: "default" | "primary" | "danger" | "update";
  disabled?: boolean;
  loading?: boolean;
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
        variant === "update" &&
          "bg-blue-600 text-white shadow-[0_10px_26px_-18px_rgba(37,99,235,0.85)] hover:bg-blue-500",
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
        {loading ? (
          <WavePhysicsLoader size="inline" theme={variant === "update" ? "dark" : "light"} />
        ) : (
          <Icon className={cn("h-[18px] w-[18px]", variant === "danger" && Icon === Square && "fill-current")} />
        )}
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
  disabled,
  onCopy,
}: {
  item: TrayTranscriptItem;
  copied: boolean;
  disabled: boolean;
  onCopy: () => void;
}) {
  const { formatDate, formatLegacyDate, t } = useI18n();
  return (
    <motion.button
      type="button"
      whileTap={disabled ? undefined : { scale: 0.985 }}
      onClick={onCopy}
      disabled={disabled}
      className={cn(
        "group flex h-[46px] w-full items-center gap-3 rounded-[12px] px-3 text-left outline-none transition-colors duration-150",
        "text-slate-950 hover:bg-slate-950/[0.055]",
        "focus-visible:ring-2 focus-visible:ring-blue-500/60 focus-visible:ring-offset-2 focus-visible:ring-offset-white/80",
        copied && "bg-emerald-50 text-emerald-700",
        disabled && "cursor-default opacity-55",
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
          {compactTranscriptTitle(item, t)}
        </span>
        <span
          className={cn(
            "mt-px block truncate text-[11px] leading-[13px]",
            copied ? "text-emerald-600" : "text-slate-500",
          )}
        >
          {copied ? t("Copied to clipboard") : compactTranscriptDetail(item, t, formatDate, formatLegacyDate)}
        </span>
      </span>
    </motion.button>
  );
}

function formatShortcut(raw: string | undefined, t: Translate): string {
  const value = String(raw || "").trim();
  if (!value) return "";
  return value
    .split("+")
    .map((part) => {
      const token = part.trim().toLowerCase();
      if (!token) return "";
      if (token === "ctrl" || token === "control") return t("Ctrl");
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
  const { formatNumber, t } = useI18n();
  const [backendReady, setBackendReady] = useState(isInitialBackendAccessReady);
  const [view, setView] = useState<TrayView>("main");
  const [status, setStatus] = useState<TrayStatus>(DEFAULT_TRAY_STATUS);
  const [appVersion, setAppVersion] = useState("");
  const [recordingShortcut, setRecordingShortcut] = useState("");
  const [meetingShortcut, setMeetingShortcut] = useState("");
  const [installing, setInstalling] = useState(false);
  const [checkingUpdates, setCheckingUpdates] = useState(false);
  const [updateCheckMessage, setUpdateCheckMessage] = useState<TrayLocalizedMessage | null>(null);
  const [progress, setProgress] = useState<DesktopUpdateProgress | null>(null);
  const [error, setError] = useState("");
  const [recentItems, setRecentItems] = useState<TrayTranscriptItem[]>([]);
  const [recentLoaded, setRecentLoaded] = useState(false);
  const [recentLoading, setRecentLoading] = useState(false);
  const [recentError, setRecentError] = useState("");
  const [copiedTranscriptId, setCopiedTranscriptId] = useState("");
  const [copyPending, setCopyPending] = useState(false);
  const shortcutLoadRequestRef = useRef(0);
  const recentRequestRef = useRef(0);
  const recentAbortRef = useRef<AbortController | null>(null);
  const copyPendingRef = useRef(false);
  const hideTimerRef = useRef<ReturnType<typeof setTimeout> | undefined>(undefined);
  const blurTimerRef = useRef<ReturnType<typeof setTimeout> | undefined>(undefined);

  const applyShortcuts = useCallback(
    (hotkey?: string, meetingHotkey?: string) => {
      setRecordingShortcut(formatShortcut(hotkey, t));
      setMeetingShortcut(formatShortcut(meetingHotkey, t));
    },
    [t],
  );

  const loadRegisteredShortcuts = useCallback(async () => {
    if (!isTauriRuntime() || !backendReady) return;
    const requestId = ++shortcutLoadRequestRef.current;
    try {
      let value = await getGlobalHotkeyStatus();
      if (!value?.hotkey) {
        value = await refreshGlobalHotkey();
      }
      if (requestId === shortcutLoadRequestRef.current) {
        applyShortcuts(value?.hotkey, value?.meetingHotkey);
      }
    } catch (error) {
      console.debug("Tray hotkey lookup failed.", error);
    }
  }, [applyShortcuts, backendReady]);

  useEffect(() => {
    document.documentElement.dataset.scriberTrayWindow = "true";
    document.body.dataset.scriberTrayWindow = "true";
    return () => {
      delete document.documentElement.dataset.scriberTrayWindow;
      delete document.body.dataset.scriberTrayWindow;
    };
  }, []);

  useEffect(() => {
    if (!isTauriRuntime()) return;
    let cancelled = false;
    void import("@tauri-apps/api/app")
      .then(({ getVersion }) => getVersion())
      .then((version) => {
        if (!cancelled) setAppVersion(String(version || "").trim());
      })
      .catch((error) => console.debug("Tray app version lookup failed.", error));
    return () => {
      cancelled = true;
    };
  }, []);

  useEffect(() => {
    let cancelled = false;
    void loadInitialBackendAccess().then(() => {
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
    let revision = 0;
    const publish = (value: TrayStatus) => {
      const next = { ...DEFAULT_TRAY_STATUS, ...value };
      setStatus((previous) =>
        previous.recordingActive === next.recordingActive &&
        previous.recordingMode === next.recordingMode &&
        previous.updateAvailable === next.updateAvailable &&
        previous.updateInstalling === next.updateInstalling &&
        previous.updateVersion === next.updateVersion &&
        previous.updateMessage === next.updateMessage
          ? previous
          : next,
      );
    };
    const readStatus = () => {
      clearTimeout(fallbackTimer);
      if (disposed) return;
      const readRevision = ++revision;
      void getTrayStatus()
        .then((value) => {
          if (!disposed && revision === readRevision && value) publish(value);
        })
        .catch((error) => console.debug("Tray status lookup failed.", error));
    };
    // A failed or stalled listener must not prevent the initial status read.
    const fallbackTimer = setTimeout(readStatus, 2_000);
    // Subscribe before reading. A newer event must win over a slow initial
    // snapshot, and access readiness must not reinstall this subscription.
    void listen<TrayStatus>("scriber-tray-status", (event) => {
      if (disposed) return;
      revision += 1;
      publish(event.payload);
    })
      .then((cleanup) => {
        if (disposed) {
          cleanup();
          return;
        }
        unlisten = cleanup;
        readStatus();
      })
      .catch((error) => {
        console.debug("Tray status listener failed.", error);
        readStatus();
      });
    return () => {
      disposed = true;
      clearTimeout(fallbackTimer);
      unlisten?.();
    };
  }, []);

  useEffect(() => {
    if (!isTauriRuntime()) return;
    const handleKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        void hideTrayPanel();
      }
    };
    const handleBlur = () => {
      clearTimeout(blurTimerRef.current);
      blurTimerRef.current = setTimeout(() => void hideTrayPanel(), 140);
    };
    const handleFocus = () => clearTimeout(blurTimerRef.current);
    window.addEventListener("keydown", handleKeyDown);
    window.addEventListener("blur", handleBlur);
    window.addEventListener("focus", handleFocus);
    return () => {
      window.removeEventListener("keydown", handleKeyDown);
      window.removeEventListener("blur", handleBlur);
      window.removeEventListener("focus", handleFocus);
      clearTimeout(blurTimerRef.current);
      clearTimeout(hideTimerRef.current);
    };
  }, []);

  const runAction = useCallback(
    async (action: TrayActionId) => {
      setError("");
      try {
        await trayAction(action);
        if (action === "toggle_live") {
          window.setTimeout(() => void hideTrayPanel(), 120);
        }
      } catch (err) {
        setError(err instanceof Error ? err.message : String(err || t("Tray action failed.")));
      }
    },
    [t],
  );

  const cancelRecentRequest = useCallback(() => {
    ++recentRequestRef.current;
    recentAbortRef.current?.abort();
    clearTimeout(hideTimerRef.current);
  }, []);

  const loadRecentTranscripts = useCallback(
    async ({ reusePending = false }: { reusePending?: boolean } = {}) => {
      if (!backendReady) return;
      // Entering the recent view can reuse this opening's pending read. A new
      // native opening or explicit refresh still invalidates older requests.
      if (reusePending && recentAbortRef.current && !recentAbortRef.current.signal.aborted) return;
      const requestId = ++recentRequestRef.current;
      recentAbortRef.current?.abort();
      const controller = new AbortController();
      recentAbortRef.current = controller;
      clearTimeout(hideTimerRef.current);
      setCopiedTranscriptId("");
      setRecentItems([]);
      setRecentLoading(true);
      setRecentError("");
      try {
        const response = await fetchWithTimeout(
          apiUrl("/api/transcripts/recent"),
          {
            credentials: "include",
            cache: "no-store",
            signal: controller.signal,
          },
          10_000,
        );
        if (!response.ok) {
          throw new Error(t("Could not load recent transcripts ({{status}}).", { status: response.status }));
        }
        const payload = (await response.json()) as TranscriptListResponse;
        if (requestId !== recentRequestRef.current) return;
        if (!Array.isArray(payload.items)) throw new Error(t("Could not load recent transcripts."));
        const seen = new Set<string>();
        setRecentItems(
          payload.items
            .filter((item) => {
              if (
                !item ||
                item.status !== "completed" ||
                typeof item.id !== "string" ||
                !/^[A-Za-z0-9_-]{1,160}$/.test(item.id) ||
                seen.has(item.id)
              )
                return false;
              seen.add(item.id);
              return true;
            })
            .slice(0, RECENT_TRANSCRIPT_LIMIT),
        );
        setRecentLoaded(true);
      } catch {
        if (requestId === recentRequestRef.current) setRecentError(t("Could not load recent transcripts."));
      } finally {
        if (requestId === recentRequestRef.current) {
          recentAbortRef.current = null;
          setRecentLoading(false);
        }
      }
    },
    [backendReady, t],
  );

  useEffect(() => {
    if (!backendReady) return;
    let disposed = false;
    let unlisten: (() => void) | undefined;
    let fallbackTimer: ReturnType<typeof setTimeout> | undefined;
    const refresh = () => {
      clearTimeout(fallbackTimer);
      if (disposed) return;
      clearTimeout(blurTimerRef.current);
      setError("");
      void loadRegisteredShortcuts();
      void loadRecentTranscripts();
    };
    if (isTauriRuntime()) {
      fallbackTimer = setTimeout(refresh, 2_000);
      // Listen before the initial read: the first show can precede WebView
      // startup, while every subsequent show (even already focused) emits this.
      void listen("scriber-tray-opened", refresh)
        .then((cleanup) => {
          if (disposed) cleanup();
          else {
            unlisten = cleanup;
            refresh();
          }
        })
        .catch(() => {
          refresh();
        });
    } else refresh();
    return () => {
      disposed = true;
      clearTimeout(fallbackTimer);
      unlisten?.();
      shortcutLoadRequestRef.current += 1;
      cancelRecentRequest();
    };
  }, [backendReady, cancelRecentRequest, loadRecentTranscripts, loadRegisteredShortcuts, t]);

  const openRecentView = useCallback(() => {
    setError("");
    setRecentError("");
    setCopiedTranscriptId("");
    setView("recent");
    void loadRecentTranscripts({ reusePending: true });
  }, [loadRecentTranscripts]);

  const copyRecentTranscript = useCallback(
    async (item: TrayTranscriptItem) => {
      const transcriptId = String(item.id || "").trim();
      if (!transcriptId || !item.contentAvailable || copyPendingRef.current) return;
      copyPendingRef.current = true;
      setCopyPending(true);
      const requestId = recentRequestRef.current;
      setRecentError("");
      setCopiedTranscriptId("");
      try {
        await trayAction(`copy_transcript:${transcriptId}`);
        if (requestId !== recentRequestRef.current) return;
        setCopiedTranscriptId(transcriptId);
        hideTimerRef.current = setTimeout(() => void hideTrayPanel(), 650);
      } catch (err) {
        if (requestId !== recentRequestRef.current) return;
        const code = err instanceof Error ? err.message : String(err);
        setRecentError(
          code === "transcript_empty"
            ? t("Empty transcript")
            : code === "transcript_unsupported_text"
              ? t("This transcript contains unsupported characters and cannot be copied.")
              : code === "clipboard_unavailable"
                ? t("Could not copy transcript.")
                : t("Transcript unavailable. Refresh recent transcripts."),
        );
      } finally {
        copyPendingRef.current = false;
        setCopyPending(false);
      }
    },
    [t],
  );

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
      setError(err instanceof Error ? err.message : String(err || t("Update installation failed.")));
    } finally {
      setInstalling(false);
    }
  }, [installing, status.updateAvailable, status.updateInstalling, t]);

  const checkForUpdates = useCallback(async () => {
    if (checkingUpdates || installing) {
      return;
    }
    setCheckingUpdates(true);
    setUpdateCheckMessage(null);
    setError("");
    try {
      const nextStatus = await checkDesktopUpdate();
      setUpdateCheckMessage({
        source: nextStatus.message || "Update check finished.",
        values: nextStatus.messageValues,
      });
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err || t("Update check failed.")));
    } finally {
      setCheckingUpdates(false);
    }
  }, [checkingUpdates, installing, t]);

  const updateDetail = useMemo(() => {
    if (installing || status.updateInstalling) {
      if (progress?.percent != null) {
        return t("{{percent}}% downloaded", { percent: formatNumber(progress.percent) });
      }
      return t(progress?.message || "Preparing installer");
    }
    if (status.updateVersion) {
      return `Scriber ${status.updateVersion}`;
    }
    return t("Install and restart");
  }, [formatNumber, installing, progress, status.updateInstalling, status.updateVersion, t]);

  const updateCheckDetail = useMemo(() => {
    if (checkingUpdates) return t("Checking GitHub");
    if (updateCheckMessage) return t(updateCheckMessage.source, updateCheckMessage.values);
    if (status.updateAvailable) return t("Check GitHub again");
    return t("Manual update check");
  }, [checkingUpdates, status.updateAvailable, updateCheckMessage, t]);

  const showUpdateInstallBanner = status.updateAvailable || status.updateInstalling || installing;
  const updateInstallTitle = (() => {
    if (installing || status.updateInstalling) return t("Installing update");
    if (status.updateVersion) return t("Install Scriber {{version}}", { version: status.updateVersion });
    return t("Install update");
  })();
  const updateInstallDetail = (() => {
    if (installing || status.updateInstalling) return updateDetail;
    return t("Download, install, and restart Scriber.");
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
            <div className="flex min-w-0 items-baseline gap-2">
              <h1 className="truncate text-[21px] font-semibold leading-6 tracking-normal text-slate-950">Scriber</h1>
              {appVersion ? (
                <span className="shrink-0 text-[10px] font-semibold tracking-[0.04em] text-slate-400">
                  v{appVersion.replace(/^v/i, "")}
                </span>
              ) : null}
            </div>
            {status.recordingActive || !showUpdateInstallBanner ? (
              <div className="mt-0.5 flex items-center gap-2 text-[11px] font-medium text-slate-500">
                <StatusIndicator status={status} />
                <span className="truncate">{statusLabel(status, t)}</span>
              </div>
            ) : null}
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
                  <WavePhysicsLoader size="inline" theme="dark" />
                ) : (
                  <Download className="h-[18px] w-[18px]" strokeWidth={2.35} aria-hidden="true" />
                )}
              </span>
              <span className="min-w-0 flex-1">
                <span className="block truncate text-[14px] font-semibold leading-[18px]">{updateInstallTitle}</span>
                <span className="mt-px block truncate text-[11px] font-medium leading-[13px] text-white/78">
                  {updateInstallDetail}
                </span>
              </span>
            </motion.button>
          </div>
        ) : null}

        <div className="min-h-0 flex-1 overflow-y-auto overscroll-contain py-2.5 pr-1">
          {view === "main" ? (
            <div className="flex flex-col gap-1.5">
              <TrayRow
                icon={status.recordingActive ? Square : Mic}
                label={status.recordingActive ? t("Stop Recording") : t("Start Live Transcription")}
                detail={status.recordingActive ? t("Live microphone is active") : t("Use the configured microphone")}
                shortcut={recordingShortcut}
                variant={status.recordingActive ? "danger" : "primary"}
                disabled={!backendReady}
                onClick={() => void runAction("toggle_live")}
              />
              <TrayRow
                icon={CalendarClock}
                label={t("Meetings")}
                detail={t("Open meeting workspace")}
                shortcut={meetingShortcut}
                onClick={() => void runAction("open_meetings")}
              />
              <TrayRow icon={Video} label={t("YouTube Transcription")} onClick={() => void runAction("open_youtube")} />
              <TrayRow icon={FileAudio} label={t("Transcribe File")} onClick={() => void runAction("open_file")} />

              <div className="my-0.5 h-px bg-slate-200/80" />

              <TrayRow
                icon={Clock3}
                label={t("Recent Transcripts")}
                detail={t("Select one to copy")}
                trailing={<ChevronRight className="h-4 w-4" />}
                disabled={!backendReady}
                onClick={openRecentView}
              />
              <TrayRow icon={MonitorUp} label={t("Open Main Window")} onClick={() => void runAction("show_window")} />

              <div className="my-0.5 h-px bg-slate-200/80" />

              <TrayRow icon={Settings} label={t("Settings")} onClick={() => void runAction("open_settings")} />
              <TrayRow
                icon={RefreshCw}
                loading={checkingUpdates}
                label={status.updateAvailable ? t("Check Again") : t("Check for Updates")}
                detail={updateCheckDetail}
                disabled={checkingUpdates || installing}
                onClick={() => void checkForUpdates()}
              />
              <TrayRow
                icon={RotateCcw}
                label={t("Restart Backend")}
                detail={t("Only restart the local worker")}
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
                  aria-label={t("Back to tray menu")}
                >
                  <ChevronLeft className="h-5 w-5" />
                </button>
                <div className="min-w-0 flex-1">
                  <div className="truncate text-[14px] font-semibold leading-[18px] text-slate-950">
                    {t("Recent Transcripts")}
                  </div>
                  <div className="truncate text-[11px] font-medium leading-[13px] text-slate-500">
                    {t("Select one to copy")}
                  </div>
                </div>
                <button
                  type="button"
                  className="flex h-8 w-8 shrink-0 items-center justify-center rounded-[10px] text-slate-700 outline-none transition-colors hover:bg-slate-950/[0.055] focus-visible:ring-2 focus-visible:ring-blue-500/60 disabled:opacity-50"
                  onClick={() => void loadRecentTranscripts()}
                  disabled={recentLoading || !backendReady}
                  aria-label={t("Refresh recent transcripts")}
                >
                  {recentLoading ? (
                    <WavePhysicsLoader size="inline" theme="light" />
                  ) : (
                    <RefreshCw className="h-4 w-4" />
                  )}
                </button>
              </div>

              <div className="h-px bg-slate-200/80" />

              {recentLoading ? (
                <TrayRow
                  icon={RefreshCw}
                  loading
                  label={t("Loading transcripts")}
                  detail={t("Checking recent history")}
                  disabled
                  onClick={() => undefined}
                />
              ) : recentItems.length > 0 ? (
                recentItems.map((item) => (
                  <RecentTranscriptRow
                    key={item.id}
                    item={item}
                    copied={copiedTranscriptId === item.id}
                    disabled={copyPending || !item.contentAvailable}
                    onCopy={() => void copyRecentTranscript(item)}
                  />
                ))
              ) : (
                <TrayRow
                  icon={Clock3}
                  label={t("No completed transcripts")}
                  detail={recentLoaded ? t("Nothing available to copy") : t("Refresh recent transcripts")}
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
              <TrayRow icon={RotateCw} label={t("Restart Application")} onClick={() => void runAction("restart_app")} />
              <TrayRow icon={LogOut} label={t("Quit Application")} onClick={() => void runAction("quit")} />
            </div>
          </div>
        ) : null}

        {error || recentError ? (
          <div className="rounded-[14px] border border-red-200 bg-red-50 px-3 py-2 text-[12px] font-medium leading-4 text-red-700">
            {t(error || recentError)}
          </div>
        ) : null}
      </motion.section>
    </main>
  );
}
