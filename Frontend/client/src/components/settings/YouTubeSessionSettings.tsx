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
          "If YouTube asks you to sign in, import an exported YouTube cookies.txt file, then retry the video. Only YouTube cookies are used, for up to two hours or until Scriber closes.",
        )}
      </p>
      <a
        className="text-[12px] underline underline-offset-2"
        href="https://github.com/yt-dlp/yt-dlp/wiki/Extractors#exporting-youtube-cookies"
        target="_blank"
        rel="noreferrer"
      >
        {t("How to export YouTube sign-in")}
      </a>
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
        <Button variant="outline" size="sm" disabled={saving} onClick={() => input.current?.click()}>
          {t("Import YouTube sign-in")}
        </Button>
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
      {error && (
        <p className="text-[12px] text-destructive" role="alert">
          {error}
        </p>
      )}
    </div>
  );
}
