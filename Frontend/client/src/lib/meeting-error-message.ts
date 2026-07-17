import type { TranslationValues } from "@/i18n";

type Translate = (source: string, values?: TranslationValues) => string;

const TEXT_GENERATION_TIMEOUT = /^Text generation timed out after (\d+(?:\.\d+)?)s\. Please try again\.$/;
const SUMMARIZATION_TIMEOUT = /^Summarization timed out after (\d+(?:\.\d+)?)s\. Please try again\.$/;

/** Localize bounded backend errors whose numeric timeout is supplied at runtime. */
export function localizeMeetingErrorMessage(message: string, t: Translate): string {
  const normalized = String(message || "").trim();
  const textGenerationTimeout = TEXT_GENERATION_TIMEOUT.exec(normalized);
  if (textGenerationTimeout) {
    return t("Text generation timed out after {{seconds}}s. Please try again.", {
      seconds: textGenerationTimeout[1],
    });
  }

  const summarizationTimeout = SUMMARIZATION_TIMEOUT.exec(normalized);
  if (summarizationTimeout) {
    return t("Summarization timed out after {{seconds}}s. Please try again.", {
      seconds: summarizationTimeout[1],
    });
  }

  return t(normalized);
}
