import type { TranslationValues } from "@/i18n";

type Translate = (source: string, values?: TranslationValues) => string;

const TEXT_GENERATION_TIMEOUT = /^Text generation timed out after (\d+(?:\.\d+)?)s\. Please try again\.$/;
const SUMMARIZATION_TIMEOUT = /^Summarization timed out after (\d+(?:\.\d+)?)s\. Please try again\.$/;

export interface MeetingAnalysisFailurePresentation {
  title: string;
  reason: string;
  safety: string;
  retryGuidance: string;
  settingsGuidance: string;
}

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

/** Hide provider internals and explain how an incomplete Meeting brief recovers. */
export function meetingAnalysisFailurePresentation(
  errorCode: string,
  errorMessage: string,
  t: Translate,
): MeetingAnalysisFailurePresentation {
  const normalizedCode = String(errorCode || "")
    .trim()
    .toLowerCase();
  const normalizedMessage = String(errorMessage || "")
    .trim()
    .toLowerCase();
  const incomplete =
    normalizedCode === "meeting_analysis_incomplete_response" ||
    /max_tokens|finish_reason|max output|partial summary was discarded/.test(normalizedMessage);
  const timedOut = normalizedCode === "meeting_analysis_timeout" || /timed out|time limit/.test(normalizedMessage);

  return {
    title: t("The meeting brief could not be completed."),
    reason: incomplete
      ? t("The AI service did not return a complete response for every part of this meeting.")
      : timedOut
        ? t("The AI service took too long to finish the meeting brief.")
        : t("The AI service could not finish the meeting brief."),
    safety: t("Your transcript, recording, speaker names, and notes are saved."),
    retryGuidance: t(
      "Select “Try meeting brief again” below. Scriber reuses completed parts and only creates the meeting brief again.",
    ),
    settingsGuidance: t("If it fails again, choose another summary model in Meeting settings and try again."),
  };
}
