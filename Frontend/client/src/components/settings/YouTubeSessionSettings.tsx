import { useEffect, useRef, useState, type ChangeEvent } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { ChevronRight } from "lucide-react";
import { Button } from "@/components/ui/button";
import { useI18n } from "@/i18n";
import { apiRequest } from "@/lib/queryClient";
import { youtubeOnlyCookieFile } from "@/lib/youtube-session";
import { isTauriRuntime } from "@/lib/backend";

const sessionKey = ["/api/youtube/session"];
const loginKey = ["/api/youtube/session/login"];
const browserConnectKey = ["/api/youtube/session/browser-connect"];
const extensionStoreUrl =
  "https://chromewebstore.google.com/detail/scriber-f%C3%BCr-youtube/ilbdnbhdihacgkaedacmndeabbiondob";

type LoginStatus = {
  state: "idle" | "opening" | "waiting" | "connected" | "failed";
  error: string;
  available: boolean;
  attempt: number;
};

type BrowserConnectStatus = {
  state: "idle" | "waiting" | "connected" | "timed_out";
  attempt: number;
  url?: string;
};

async function openExternalUrl(url: string): Promise<void> {
  if (isTauriRuntime()) {
    const { openUrl } = await import("@tauri-apps/plugin-opener");
    await openUrl(url);
    return;
  }
  window.open(url, "_blank", "noopener,noreferrer");
}

async function focusAppWindowBestEffort(): Promise<void> {
  if (!isTauriRuntime()) return;
  try {
    const { getCurrentWindow } = await import("@tauri-apps/api/window");
    const current = getCurrentWindow();
    await current.unminimize().catch(() => undefined);
    await current.setFocus().catch(() => undefined);
  } catch {
    // Focusing back is purely a UX convenience.
  }
}

export function YouTubeSessionSettings({
  onConnected,
  recoveryKey,
  videoUrl,
}: {
  onConnected?: () => void | Promise<void>;
  recoveryKey?: string;
  videoUrl?: string;
}) {
  const { t } = useI18n();
  const queryClient = useQueryClient();
  const input = useRef<HTMLInputElement>(null);
  const pending = useRef(false);
  const ownAttempt = useRef<{ id: number; recoveryKey?: string } | null>(null);
  const ownBrowserAttempt = useRef<{ id: number; recoveryKey?: string } | null>(null);
  const mounted = useRef(true);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");
  const browserConnect = useQuery<BrowserConnectStatus>({
    queryKey: browserConnectKey,
    staleTime: 0,
    refetchInterval: (query) => (query.state.data?.state === "waiting" ? 1_000 : false),
  });
  const connectingBrowser = browserConnect.data?.state === "waiting";
  const status = useQuery<{ connected: boolean }>({
    queryKey: sessionKey,
    refetchInterval: connectingBrowser ? 1_500 : 30_000,
  });
  const login = useQuery<LoginStatus>({
    queryKey: loginKey,
    staleTime: 0,
    refetchInterval: (query) => (["opening", "waiting"].includes(query.state.data?.state ?? "") ? 1_000 : false),
  });
  const signingIn = login.data?.state === "opening" || login.data?.state === "waiting";

  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
      ownAttempt.current = null;
      ownBrowserAttempt.current = null;
    };
  }, []);

  useEffect(() => {
    if (login.data?.state !== "connected") return;
    void queryClient.invalidateQueries({ queryKey: sessionKey });
    if (ownAttempt.current?.id !== login.data.attempt || ownAttempt.current.recoveryKey !== recoveryKey) return;
    ownAttempt.current = null;
    void focusAppWindowBestEffort();
    void onConnected?.();
  }, [login.data, onConnected, queryClient, recoveryKey]);

  useEffect(() => {
    if (browserConnect.data?.state !== "connected") return;
    void queryClient.invalidateQueries({ queryKey: sessionKey });
    if (
      ownBrowserAttempt.current?.id !== browserConnect.data.attempt ||
      ownBrowserAttempt.current.recoveryKey !== recoveryKey
    ) {
      return;
    }
    ownBrowserAttempt.current = null;
    void focusAppWindowBestEffort();
    void onConnected?.();
  }, [browserConnect.data, onConnected, queryClient, recoveryKey]);

  useEffect(() => {
    if (!status.data?.connected || !ownBrowserAttempt.current) return;
    if (ownBrowserAttempt.current.recoveryKey !== recoveryKey) return;
    ownBrowserAttempt.current = null;
    void queryClient.invalidateQueries({ queryKey: browserConnectKey });
    void focusAppWindowBestEffort();
    void onConnected?.();
  }, [status.data?.connected, onConnected, queryClient, recoveryKey]);

  async function startBrowserConnect() {
    if (pending.current) return;
    pending.current = true;
    setSaving(true);
    setError("");
    try {
      const response = await apiRequest(
        "POST",
        browserConnectKey[0],
        videoUrl ? { url: videoUrl } : {},
      );
      const result: BrowserConnectStatus = await response.json();
      if (!mounted.current) return;
      ownBrowserAttempt.current = { id: result.attempt, recoveryKey };
      queryClient.setQueryData(browserConnectKey, result);
      if (result.url) {
        await openExternalUrl(result.url);
      }
    } catch {
      ownBrowserAttempt.current = null;
      setError(t("Your browser could not be opened. Please try again."));
    } finally {
      pending.current = false;
      setSaving(false);
    }
  }

  async function cancelBrowserConnect() {
    if (pending.current) return;
    pending.current = true;
    ownBrowserAttempt.current = null;
    setSaving(true);
    setError("");
    try {
      const response = await apiRequest("DELETE", browserConnectKey[0]);
      queryClient.setQueryData(browserConnectKey, await response.json());
    } catch {
      setError(t("Browser connection could not be cancelled. Please try again."));
    } finally {
      pending.current = false;
      setSaving(false);
    }
  }

  async function startLogin() {
    if (pending.current) return;
    pending.current = true;
    setSaving(true);
    setError("");
    try {
      const response = await apiRequest("POST", loginKey[0]);
      const result: LoginStatus = await response.json();
      if (!mounted.current) return;
      ownAttempt.current = { id: result.attempt, recoveryKey };
      queryClient.setQueryData(loginKey, result);
    } catch {
      setError(t("The sign-in window could not be opened. Please try again."));
    } finally {
      pending.current = false;
      setSaving(false);
    }
  }

  async function cancelLogin() {
    if (pending.current) return;
    pending.current = true;
    ownAttempt.current = null;
    setSaving(true);
    setError("");
    try {
      const response = await apiRequest("DELETE", loginKey[0]);
      queryClient.setQueryData(loginKey, await response.json());
    } catch {
      setError(t("Sign-in could not be cancelled. Please try again."));
    } finally {
      pending.current = false;
      setSaving(false);
    }
  }

  async function importSession(event: ChangeEvent<HTMLInputElement>) {
    const file = event.target.files?.[0];
    event.target.value = "";
    if (!file || pending.current) return;
    pending.current = true;
    setSaving(true);
    setError("");
    try {
      if (file.size > 64 * 1024) throw new Error("oversized");
      const response = await apiRequest("POST", "/api/youtube/session", {
        cookies: youtubeOnlyCookieFile(await file.text()),
      });
      queryClient.setQueryData(sessionKey, await response.json());
      if (mounted.current) void onConnected?.();
    } catch {
      setError(t("The YouTube sign-in file is invalid or could not be imported."));
    } finally {
      pending.current = false;
      setSaving(false);
    }
  }

  async function disconnect() {
    if (pending.current) return;
    pending.current = true;
    ownAttempt.current = null;
    ownBrowserAttempt.current = null;
    setSaving(true);
    setError("");
    try {
      const response = await apiRequest("DELETE", "/api/youtube/session");
      queryClient.setQueryData(sessionKey, await response.json());
      await queryClient.invalidateQueries({ queryKey: loginKey });
      await queryClient.invalidateQueries({ queryKey: browserConnectKey });
    } catch {
      setError(t("YouTube sign-in could not be cleared. Please try again."));
    } finally {
      pending.current = false;
      setSaving(false);
    }
  }

  async function openExtensionStore() {
    try {
      const { openUrl } = await import("@tauri-apps/plugin-opener");
      await openUrl(extensionStoreUrl);
    } catch {
      setError(t("The Chrome Web Store could not be opened. Please try again."));
    }
  }

  return (
    <div className="space-y-2 border-t border-slate-200/80 pt-3 dark:border-[var(--workspace-border)]">
      <p className="text-[13px] font-semibold">{t("YouTube sign-in")}</p>
      <p className="text-[12px] text-muted-foreground">
        {t(
          "Reuse your existing YouTube sign-in from your normal browser via the Scriber add-on. Scriber uses only your YouTube sign-in, for up to two hours or until Scriber closes.",
        )}
      </p>
      <p className="text-[12px]" role="status">
        {status.isError
          ? t("YouTube sign-in status is unavailable.")
          : status.data?.connected
            ? t("YouTube sign-in loaded")
            : t("No YouTube sign-in loaded")}
      </p>
      <input
        ref={input}
        type="file"
        accept=".txt,text/plain"
        aria-label={t("YouTube sign-in file")}
        className="sr-only"
        onChange={(event) => {
          void importSession(event);
        }}
      />
      <div className="flex flex-wrap gap-2">
        {connectingBrowser ? (
          <Button
            variant="outline"
            size="sm"
            disabled={saving}
            onClick={() => {
              void cancelBrowserConnect();
            }}
          >
            {t("Cancel browser connection")}
          </Button>
        ) : (
          <Button
            variant="default"
            size="sm"
            disabled={saving || signingIn}
            onClick={() => {
              void startBrowserConnect();
            }}
          >
            {t("Connect from browser")}
          </Button>
        )}
        {status.data?.connected && (
          <Button
            variant="ghost"
            size="sm"
            disabled={saving}
            onClick={() => {
              void disconnect();
            }}
          >
            {t("Clear YouTube sign-in")}
          </Button>
        )}
      </div>
      {connectingBrowser && (
        <p className="text-[12px] text-muted-foreground" role="status">
          {onConnected
            ? t(
                "YouTube opened in your browser. With add-on version 0.2.2 or later, your sign-in connects automatically and restarts this video. On older versions, click “Transcribe with Scriber” on the opened page.",
              )
            : t(
                "YouTube opened in your browser. With add-on version 0.2.2 or later, your sign-in connects automatically. On older versions, click the Scriber add-on icon on the opened page.",
              )}
        </p>
      )}
      {browserConnect.data?.state === "timed_out" && (
        <p className="text-[12px] text-destructive" role="alert">
          {t("Browser connection timed out. Make sure the Scriber add-on is installed and YouTube sign-in is allowed, then try again.")}
        </p>
      )}
      <Button asChild variant="link" size="sm" className="h-auto px-0">
        <a
          href={extensionStoreUrl}
          target="_blank"
          rel="noopener noreferrer"
          onClick={(event) => {
            if (isTauriRuntime()) {
              event.preventDefault();
              void openExtensionStore();
            }
          }}
        >
          {t("Open add-on in Chrome Web Store")}
        </a>
      </Button>
      <p className="text-[12px] text-muted-foreground">
        {t("Confirm the installation in Chrome, then allow YouTube sign-in once in the add-on. Future videos from inside Scriber can then connect with one click.")}
      </p>
      <details className="settings-disclosure text-[12px] text-muted-foreground">
        <summary>
          <ChevronRight className="settings-disclosure__chevron" aria-hidden="true" />
          {t("Alternative: private sign-in window or sign-in file")}
        </summary>
        <div className="mt-2 space-y-2">
          <p className="text-[12px] text-muted-foreground">
            {t(
              "Without the browser add-on you can try a private browser window or import a YouTube sign-in file. Note: Google may reject direct sign-in in isolated browser windows.",
            )}
          </p>
          <div className="flex flex-wrap gap-2">
            {signingIn ? (
              <Button
                variant="outline"
                size="sm"
                disabled={saving}
                onClick={() => {
                  void cancelLogin();
                }}
              >
                {t("Cancel sign-in")}
              </Button>
            ) : (
              <Button
                variant="outline"
                size="sm"
                disabled={saving || connectingBrowser}
                onClick={() => {
                  void startLogin();
                }}
              >
                {t("Sign in to YouTube")}
              </Button>
            )}
            <Button
              variant="outline"
              size="sm"
              disabled={saving || signingIn || connectingBrowser}
              onClick={() => input.current?.click()}
            >
              {t("Import YouTube sign-in")}
            </Button>
          </div>
          {signingIn && (
            <p className="text-[12px] text-muted-foreground" role="status">
              {onConnected
                ? t("Complete sign-in in the browser. This video will then restart automatically.")
                : t("Complete sign-in in the browser. Scriber will connect automatically.")}
            </p>
          )}
          {login.data?.state === "failed" && (
            <p className="text-[12px] text-destructive" role="alert">
              {login.data.error === "browser_missing"
                ? t("Chrome or Edge is needed for this sign-in. You can also import a sign-in file below.")
                : login.data.error === "timed_out"
                  ? t("Sign-in took too long. Please try again.")
                  : t("The sign-in window was closed or could not connect. Please try again.")}
            </p>
          )}
        </div>
      </details>
      {error && (
        <p className="text-[12px] text-destructive" role="alert">
          {error}
        </p>
      )}
    </div>
  );
}
