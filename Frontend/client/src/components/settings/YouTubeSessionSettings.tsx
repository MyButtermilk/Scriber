import { useEffect, useRef, useState, type ChangeEvent } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { Button } from "@/components/ui/button";
import { useI18n } from "@/i18n";
import { apiRequest } from "@/lib/queryClient";
import { youtubeOnlyCookieFile } from "@/lib/youtube-session";
import { isTauriRuntime } from "@/lib/backend";

const sessionKey = ["/api/youtube/session"];
const loginKey = ["/api/youtube/session/login"];
const extensionStoreUrl =
  "https://chromewebstore.google.com/detail/scriber-f%C3%BCr-youtube/ilbdnbhdihacgkaedacmndeabbiondob";

type LoginStatus = {
  state: "idle" | "opening" | "waiting" | "connected" | "failed";
  error: string;
  available: boolean;
  attempt: number;
};

export function YouTubeSessionSettings({ onConnected }: { onConnected?: () => void | Promise<void> }) {
  const { t } = useI18n();
  const queryClient = useQueryClient();
  const input = useRef<HTMLInputElement>(null);
  const pending = useRef(false);
  const ownAttempt = useRef<number | null>(null);
  const mounted = useRef(true);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");
  const status = useQuery<{ connected: boolean }>({ queryKey: sessionKey, refetchInterval: 30_000 });
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
    };
  }, []);

  useEffect(() => {
    if (login.data?.state !== "connected") return;
    void queryClient.invalidateQueries({ queryKey: sessionKey });
    if (ownAttempt.current !== login.data.attempt) return;
    ownAttempt.current = null;
    void onConnected?.();
  }, [login.data, onConnected, queryClient]);

  async function startLogin() {
    if (pending.current) return;
    pending.current = true;
    setSaving(true);
    setError("");
    try {
      const response = await apiRequest("POST", loginKey[0]);
      const result: LoginStatus = await response.json();
      if (!mounted.current) return;
      ownAttempt.current = result.attempt;
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
    setSaving(true);
    setError("");
    try {
      const response = await apiRequest("DELETE", "/api/youtube/session");
      queryClient.setQueryData(sessionKey, await response.json());
      await queryClient.invalidateQueries({ queryKey: loginKey });
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
          "Sign in to YouTube in a private browser window. No add-on is required. Scriber uses only your YouTube sign-in, for up to two hours or until Scriber closes.",
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
            disabled={saving}
            onClick={() => {
              void startLogin();
            }}
          >
            {t("Sign in to YouTube")}
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
      <p className="text-[12px] text-muted-foreground">
        {t(
          "Optional: the Scriber browser add-on can reuse your YouTube sign-in when you send a video, reducing repeated sign-in prompts.",
        )}
      </p>
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
        {t("Confirm the installation in Chrome, then allow YouTube sign-in in the add-on when sending a video.")}
      </p>
      <details className="text-[12px] text-muted-foreground">
        <summary className="cursor-pointer">{t("Advanced: import a sign-in file")}</summary>
        <Button
          className="mt-2"
          variant="outline"
          size="sm"
          disabled={saving || signingIn}
          onClick={() => input.current?.click()}
        >
          {t("Import YouTube sign-in")}
        </Button>
      </details>
      {error && (
        <p className="text-[12px] text-destructive" role="alert">
          {error}
        </p>
      )}
    </div>
  );
}
