export type SummaryDocumentLanguage = "de" | "en";

const LANGUAGE_MARKERS: Record<SummaryDocumentLanguage, ReadonlySet<string>> = {
  de: new Set(["aber", "auch", "dass", "der", "die", "ein", "eine", "für", "ist", "mit", "nicht", "und", "von", "wir"]),
  en: new Set(["and", "are", "for", "from", "have", "is", "not", "that", "the", "this", "to", "we", "with", "you"]),
};

function languageHint(value: string | null | undefined): SummaryDocumentLanguage | null {
  const code = String(value || "").trim().toLocaleLowerCase().replace("_", "-").split("-", 1)[0];
  return code === "de" || code === "en" ? code : null;
}

export function summaryDocumentLanguage(
  summaryText: string,
  fallbackLanguage?: string | null,
): SummaryDocumentLanguage {
  const words = String(summaryText || "")
    .toLocaleLowerCase()
    .match(/[A-Za-zÀ-ÖØ-öø-ÿ]+/g) || [];
  const scores = (Object.keys(LANGUAGE_MARKERS) as SummaryDocumentLanguage[])
    .map((language) => ({
      language,
      score: words.reduce(
        (total, word) => total + (LANGUAGE_MARKERS[language].has(word) ? 1 : 0),
        0,
      ),
    }))
    .sort((left, right) => right.score - left.score);
  if (scores[0].score >= 2 && scores[0].score > scores[1].score) {
    return scores[0].language;
  }
  return languageHint(fallbackLanguage) || "en";
}

export function summaryTableOfContentsTitle(
  summaryText: string,
  fallbackLanguage?: string | null,
): "Inhaltsverzeichnis" | "Table of Contents" {
  return summaryDocumentLanguage(summaryText, fallbackLanguage) === "de"
    ? "Inhaltsverzeichnis"
    : "Table of Contents";
}
