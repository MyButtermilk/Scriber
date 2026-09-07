import { AlertCircle, FileAudio, FileVideo } from "lucide-react";
import { useI18n } from "@/i18n";
import type { FileUploadQueueItem } from "@/lib/file-upload-store";

export function FileImportQueue({ items }: { items: FileUploadQueueItem[] }) {
  const { t, formatNumber } = useI18n();
  const visible = items.filter((item) => item.status !== "completed");
  if (!visible.length) return null;
  return (
    <section className="mb-7 space-y-3" aria-labelledby="file-import-heading">
      <div className="flex flex-wrap items-baseline justify-between gap-2 px-1">
        <h2 id="file-import-heading" className="font-heading text-[17px] font-semibold">
          {t("Preparing files")}
        </h2>
        <p className="text-xs text-muted-foreground">{t("Two files at a time · you can add more anytime")}</p>
      </div>
      <ul className="file-import-list divide-y divide-border/60 overflow-hidden rounded-[20px] border border-border/60">
        {visible.map((item) => {
          const failed = item.status === "failed";
          const preparing = item.status === "server_processing";
          const uploading = item.status === "uploading";
          const Icon = failed
            ? AlertCircle
            : /\.(mp4|mov|webm|avi|mkv|m4v|flv|wmv)$/i.test(item.fileName)
              ? FileVideo
              : FileAudio;
          return (
            <li key={item.id} className="file-import-row px-4 py-4 md:px-5" data-status={item.status}>
              <div className="flex min-w-0 items-start gap-3.5">
                <div
                  className={`file-history-icon flex h-10 w-10 shrink-0 items-center justify-center rounded-xl ${failed ? "text-destructive" : "text-primary"}`}
                >
                  <Icon className="h-5 w-5" aria-hidden="true" />
                </div>
                <div className="min-w-0 flex-1 space-y-2.5">
                  <div className="flex min-w-0 items-baseline gap-3">
                    <span className="min-w-0 flex-1 truncate text-[14px] font-medium" title={item.fileName}>
                      {item.fileName}
                    </span>
                    {uploading && (
                      <span className="shrink-0 font-mono text-xs tabular-nums text-primary">
                        {formatNumber(item.progress / 100, { style: "percent", maximumFractionDigits: 0 })}
                      </span>
                    )}
                  </div>
                  {(uploading || preparing) && (
                    <div
                      className={`file-import-progress ${preparing ? "is-preparing" : ""}`}
                      role="progressbar"
                      aria-label={t("Import progress for {{file}}", { file: item.fileName })}
                      aria-valuemin={0}
                      aria-valuemax={100}
                      aria-valuenow={preparing ? undefined : item.progress}
                      aria-valuetext={t(item.statusText, item.statusValues)}
                    >
                      <span style={preparing ? undefined : { transform: `scaleX(${item.progress / 100})` }} />
                    </div>
                  )}
                  <p
                    className={`text-xs leading-5 ${failed ? "break-words text-destructive" : "text-muted-foreground"}`}
                    role={failed ? "alert" : undefined}
                  >
                    {failed ? t(item.error) : t(item.statusText, item.statusValues)}
                  </p>
                </div>
              </div>
            </li>
          );
        })}
      </ul>
    </section>
  );
}
