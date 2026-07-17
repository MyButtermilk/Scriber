import type { TranslationValues } from "@/i18n";

type Translate = (source: string, values?: TranslationValues) => string;
type FormatNumber = (value: number, options?: Intl.NumberFormatOptions) => string;

function localizeByteLabel(value: string, formatNumber: FormatNumber): string {
  const match = /^(\d+(?:\.\d+)?)\s*(B|KB|MB|GB)$/.exec(value.trim());
  if (!match) return value;

  const fractionDigits = match[1].includes(".") ? match[1].split(".")[1].length : 0;
  return `${formatNumber(Number(match[1]), {
    minimumFractionDigits: fractionDigits,
    maximumFractionDigits: fractionDigits,
  })} ${match[2]}`;
}

export function localizeOnnxDownloadMessage(
  value: string | null | undefined,
  t: Translate,
  formatNumber: FormatNumber,
): string {
  const source = value?.trim() || "Downloading...";

  const modelFilesMatch = /^Downloading model files \(([^)]+)\)\. This can take a while\.\.\.$/.exec(source);
  if (modelFilesMatch) {
    return t("Downloading model files ({{quantization}}). This can take a while...", {
      quantization: modelFilesMatch[1],
    });
  }

  const fileCountMatch = /^Downloading files (\d+)\/(\d+)\.\.\.$/.exec(source);
  if (fileCountMatch) {
    return t("Downloading files {{current}}/{{total}}...", {
      current: formatNumber(Number(fileCountMatch[1])),
      total: formatNumber(Number(fileCountMatch[2])),
    });
  }

  const byteProgressMatch = /^Downloading (.+) \(([^/()]+)\/([^()]+)\)$/.exec(source);
  if (byteProgressMatch) {
    return t("Downloading {{file}} ({{downloaded}}/{{total}})", {
      file: byteProgressMatch[1],
      downloaded: localizeByteLabel(byteProgressMatch[2], formatNumber),
      total: localizeByteLabel(byteProgressMatch[3], formatNumber),
    });
  }

  const failureMatch = /^Download failed:\s*(.+)$/.exec(source);
  if (failureMatch) {
    return t("Download failed: {{error}}", { error: failureMatch[1] });
  }

  return t(source);
}
