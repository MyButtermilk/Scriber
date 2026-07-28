import { AlertTriangle, Check, Loader2 } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Textarea } from "@/components/ui/textarea";
import { useI18n } from "@/i18n";
import type { UseMeetingNotesAutosaveResult } from "./useMeetingNotesAutosave";

interface MeetingNotesEditorProps {
  autosave: UseMeetingNotesAutosaveResult;
  rows?: number;
  textareaClassName?: string;
}

export function MeetingNotesEditor({ autosave, rows = 5, textareaClassName }: MeetingNotesEditorProps) {
  const { t } = useI18n();
  const { body, error, retry, setBody, status, teardownSafe } = autosave;

  return (
    <div className="mt-3 space-y-2">
      <Textarea
        aria-label={t("Live notes")}
        value={body}
        onChange={(event) => setBody(event.target.value)}
        placeholder={t("Capture decisions and follow-ups…")}
        rows={rows}
        className={textareaClassName}
      />
      {autosave.isDirty && !teardownSafe ? (
        <p className="flex items-center text-xs text-amber-700 dark:text-amber-300" role="alert">
          <AlertTriangle className="mr-2 h-3.5 w-3.5 shrink-0" />
          {t("This note is too large for a shutdown save. Wait for Saved before closing Scriber.")}
        </p>
      ) : null}
      {status === "error" ? (
        <div className="flex items-center justify-between gap-3 text-xs text-destructive" role="alert">
          <span className="flex min-w-0 items-center">
            <AlertTriangle className="mr-2 h-3.5 w-3.5 shrink-0" />
            <span className="truncate" title={error?.message}>
              {t("Note was not saved")}
            </span>
          </span>
          <Button className="h-7 shrink-0 px-2 text-xs" onClick={retry} size="sm" type="button" variant="outline">
            {t("Retry")}
          </Button>
        </div>
      ) : (
        <p aria-live="polite" className="flex items-center text-xs text-muted-foreground">
          {status === "saving" ? (
            <Loader2 className="mr-2 h-3.5 w-3.5 animate-spin" />
          ) : (
            <Check className="mr-2 h-3.5 w-3.5" />
          )}
          {status === "saving"
            ? t("Saving…")
            : status === "dirty"
              ? t("Unsaved changes")
              : t("Notes autosave and AI regeneration never overwrites them.")}
        </p>
      )}
    </div>
  );
}
