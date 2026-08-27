import type { useI18n } from "@/i18n";

export function localizedYoutubeProcessingStep(
  value: string | null | undefined,
  fallback: string,
  t: ReturnType<typeof useI18n>["t"],
  formatNumber: ReturnType<typeof useI18n>["formatNumber"],
): string {
  const source = String(value || fallback).trim();
  const audioRetryMatch = /^Retrying audio download in ([\d.,]+)s \((\d+)\/(\d+)\)$/.exec(source);
  if (audioRetryMatch) {
    const seconds = Number(audioRetryMatch[1].replace(",", "."));
    return t("Retrying audio download in {{seconds}}s ({{attempt}}/{{total}})", {
      seconds: Number.isFinite(seconds) ? formatNumber(seconds, { maximumFractionDigits: 2 }) : audioRetryMatch[1],
      attempt: formatNumber(Number(audioRetryMatch[2])),
      total: formatNumber(Number(audioRetryMatch[3])),
    });
  }

  const retryMatch = /^Retrying in ([\d.,]+)s \((\d+)\/(\d+)\)$/.exec(source);
  if (retryMatch) {
    const seconds = Number(retryMatch[1].replace(",", "."));
    return t("Retrying in {{seconds}}s ({{attempt}}/{{total}})", {
      seconds: Number.isFinite(seconds) ? formatNumber(seconds, { maximumFractionDigits: 2 }) : retryMatch[1],
      attempt: formatNumber(Number(retryMatch[2])),
      total: formatNumber(Number(retryMatch[3])),
    });
  }

  const downloadMatch = /^Downloading\.\.\.\s+([\d.,]+)%(.*)$/.exec(source);
  if (downloadMatch) {
    const percentage = Number(downloadMatch[1].replace(",", "."));
    const formattedPercentage = Number.isFinite(percentage)
      ? formatNumber(percentage / 100, { style: "percent", maximumFractionDigits: 1 })
      : `${downloadMatch[1]}%`;
    const technicalSuffix = downloadMatch[2].replace(" • ETA ", ` • ${t("ETA")} `);
    return `${t("Downloading… {{percent}}", { percent: formattedPercentage })}${technicalSuffix}`;
  }

  if (source.startsWith("Error: ")) {
    return `${t("Error")}: ${source.slice("Error: ".length)}`;
  }
  return t(source);
}
