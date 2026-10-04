import { useRef, useState, type ChangeEvent } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { Button } from "@/components/ui/button";
import { useI18n } from "@/i18n";
import { apiRequest } from "@/lib/queryClient";
import { youtubeOnlyCookieFile } from "@/lib/youtube-session";

const sessionKey = ["/api/youtube/session"];

export function YouTubeSessionSettings() {
  const { t } = useI18n();
  const queryClient = useQueryClient();
  const input = useRef<HTMLInputElement>(null);
  const pending = useRef(false);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");
  const status = useQuery<{ connected: boolean }>({ queryKey: sessionKey, refetchInterval: 30_000 });

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
    setSaving(true);
    setError("");
    try {
      const response = await apiRequest("DELETE", "/api/youtube/session");
      queryClient.setQueryData(sessionKey, await response.json());
    } catch {
      setError(t("YouTube sign-in could not be cleared. Please try again."));
    } finally {
      pending.current = false;
      setSaving(false);
    }
  }

  return (
    <div className="space-y-2 border-t border-slate-200/80 pt-3 dark:border-[var(--workspace-border)]">
      <p className="text-[13px] font-semibold">{t("YouTube sign-in")}</p>
      <p className="text-[12px] text-muted-foreground">
        {t(
          "Start the video with the Scriber browser extension. Allow YouTube sign-in once in its toolbar popup; future videos use it automatically. The session stays on this computer for up to two hours or until Scriber closes.",
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
      <details className="text-[12px] text-muted-foreground">
        <summary className="cursor-pointer">{t("Advanced: import a sign-in file")}</summary>
        <Button className="mt-2" variant="outline" size="sm" disabled={saving} onClick={() => input.current?.click()}>
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
