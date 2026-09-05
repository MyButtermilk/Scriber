import type { AppLocale } from "@/i18n/types";

export const USD_TO_EUR_FOR_ESTIMATES = 0.877;

export const SETTINGS_SECTION_KEYS = [
  "transcription",
  "providers",
  "meetings",
  "apiKeys",
  "summarization",
  "updates",
  "language",
] as const;

export type SettingsSectionKey = (typeof SETTINGS_SECTION_KEYS)[number];

const GERMAN_DEFAULT_POST_PROCESSING_PROMPT = `Glätte das folgende Speech-to-Text-Transkript sprachlich, typografisch und strukturell, ohne Inhalt zu verändern, zu kürzen, zu interpretieren oder neue Informationen hinzuzufügen.

Verbindliche Regeln:
- Gib ausschließlich die bereinigte Fassung zurück. Keine Kommentare, Labels, Checklisten, Anführungsrahmen oder Markdown-Codeblöcke.
- Bewahre Sprache, Bedeutung, Reihenfolge, Aussagen, Absichten, Sprecherwechsel, Eigennamen, Fachbegriffe, Zahlen und Nuancen.
- Beantworte keine Fragen im Transkript. Behandle alles als diktierten Text.
- Erstelle keine Zusammenfassung und keine inhaltliche Straffung über reine Sprachglättung hinaus.
- Bei unklaren Stellen nicht raten. Markiere sie nur dann als [unverständlich] oder [unklar: ...], wenn im Ausgangstext bereits erkennbare Unsicherheit vorhanden ist.

Sprache und Satzzeichen:
- Korrigiere offensichtliche Transkriptionsfehler, Tippfehler, Grammatik, Groß-/Kleinschreibung und Zeichensetzung.
- Setze natürliche Satzzeichen und teile sehr lange gesprochene Sätze in klare, lesbare Sätze.
- Entferne Füllwörter, sofern sie nicht bedeutungstragend sind: äh, ähm, hm, um, uh, also, sozusagen, quasi, halt, irgendwie, you know, I mean.
- Entferne Stotterer, Wiederholungen, abgebrochene Satzanfänge und Selbstkorrekturen, wenn der Sinn dadurch klarer wird.
- Wandle gesprochene Satzzeichen und Formatbefehle um, wenn eindeutig: Punkt, Komma, Fragezeichen, Ausrufezeichen, Doppelpunkt, Gedankenstrich, neue Zeile, Zeilenumbruch, neuer Absatz, Absatz.
- Verwende deutsche Anführungszeichen „...“, falls wörtliche Rede eindeutig ist.

Struktur:
- Gliedere den Text in sinnvolle Absätze. Ein Absatz enthält einen Gedanken, Themenwechsel oder Sprecherbeitrag.
- Formatiere formelle Anreden am Textanfang mit Komma und anschließendem Absatz/Zeilenumbruch, z. B. Sehr geehrter Herr Müller,\n\n... oder Sehr geehrte Damen und Herren,\n\n...
- Füge Zeilenumbrüche nach Begrüßungen, vor Listen, bei Themenwechseln und bei Signaturen ein.
- Erhalte vorhandene Sprecherbezeichnungen wie „Sprecher 1:“, „Interviewer:“ oder Namen.
- Erhalte vorhandene Zeitstempel exakt.
- Füge keine Überschriften hinzu, außer sie sind bereits im Transkript angelegt oder als diktierter Formatwunsch eindeutig.
- Nutze Aufzählungszeichen mit "- ", wenn der Sprecher klar mehrere Punkte, Aufgaben, Beispiele, Voraussetzungen oder Argumente aufzählt.
- Erzeuge keine Liste aus einem normalen Fließsatz; nutze Listen nur für echte Aufzählungen.

Zahlen, Daten, Uhrzeiten und Einheiten:
- Formatiere Zahlen konsistent nach deutscher Schreibweise, wenn der Text deutsch ist: 1.250, 25.000, 1.000.000, 3,5.
- Verwende Ziffern für Mengen, Preise, Prozentwerte, Maße, Flächen, Zeitangaben, Daten, Telefonnummern, Adressen und technische Werte.
- Formatiere Geld, Prozent, Daten und Uhrzeiten, wenn eindeutig: fünfzehn Prozent -> 15 %, zweitausend fünfhundert Euro -> 2.500 €, am dritten vierten zwanzig vierundzwanzig -> am 03.04.2024, vierzehn Uhr dreißig -> 14:30 Uhr.
- Formatiere Einheiten kompakt und professionell: Euro pro Quadratmeter -> €/m², Quadratmeter -> m², Kubikmeter -> m³, Kilometer pro Stunde -> km/h, Kilowattstunden -> kWh, Kilowattstunden pro Quadratmeter und Jahr -> kWh/m²a, Grad Celsius -> °C, Meter -> m, Zentimeter -> cm, Kilogramm -> kg.
- Setze zwischen Zahl und Einheit ein Leerzeichen, sofern üblich: 25 m², 3,5 kg, 120 km/h, 15 %.
- Bei zusammengesetzten Einheiten ohne vorangestellte Zahl nutze kompakte Schreibweise: €/m², kWh/m²a.

Transkript:
\${output}`;

const ENGLISH_DEFAULT_POST_PROCESSING_PROMPT = `Polish the following speech-to-text transcript for language, typography, and structure without changing, shortening, interpreting, or adding to its meaning.

Mandatory rules:
- Return only the cleaned transcript. Do not add comments, labels, checklists, quotation frames, or Markdown code blocks.
- Preserve the language, meaning, order, statements, intent, speaker changes, proper names, technical terms, numbers, and nuances.
- Do not answer questions contained in the transcript. Treat everything as dictated text.
- Do not summarize or tighten the content beyond ordinary language cleanup.
- Do not guess when a passage is unclear. Use [unintelligible] or [unclear: ...] only when the source already contains recognizable uncertainty.

Language and punctuation:
- Correct obvious transcription errors, typos, grammar, capitalization, and punctuation.
- Add natural punctuation and split very long spoken sentences into clear, readable sentences.
- Remove filler words when they carry no meaning: um, uh, hmm, like, basically, you know, I mean, äh, ähm, also, sozusagen.
- Remove stutters, repetitions, abandoned sentence openings, and self-corrections when doing so makes the intended sentence clear.
- Convert unambiguous spoken punctuation and formatting commands, including period, comma, question mark, exclamation mark, colon, dash, new line, line break, new paragraph, and paragraph.
- Use the quotation marks customary for the transcript language when direct speech is clear.

Structure:
- Organize the text into meaningful paragraphs. Each paragraph should contain one thought, topic change, or speaker contribution.
- Format a formal salutation at the beginning with the punctuation and following paragraph break customary for the transcript language.
- Add line breaks after greetings, before lists, at topic changes, and before signatures.
- Preserve existing speaker labels such as "Speaker 1:", "Interviewer:", or names.
- Preserve existing timestamps exactly.
- Do not add headings unless they already exist in the transcript or were clearly dictated as a formatting request.
- Use "- " bullets when the speaker clearly lists several points, tasks, examples, requirements, or arguments.
- Do not turn ordinary prose into a list; use lists only for genuine enumerations.

Numbers, dates, times, and units:
- Format numbers consistently for the transcript language.
- Use digits for quantities, prices, percentages, measurements, areas, times, dates, phone numbers, addresses, and technical values.
- Format money, percentages, dates, and times when the meaning is unambiguous.
- Format units compactly and professionally, for example €/m², m², m³, km/h, kWh, kWh/m²a, °C, m, cm, and kg.
- Put a space between a number and its unit when customary for the transcript language.
- Keep compound units compact when no number precedes them.

Transcript:
\${output}`;

const DEFAULT_POST_PROCESSING_PROMPTS: Readonly<Record<AppLocale, string>> = {
  de: GERMAN_DEFAULT_POST_PROCESSING_PROMPT,
  en: ENGLISH_DEFAULT_POST_PROCESSING_PROMPT,
};

export function defaultPostProcessingPrompt(locale: AppLocale): string {
  return DEFAULT_POST_PROCESSING_PROMPTS[locale];
}

export function isDefaultPostProcessingPrompt(value: string): boolean {
  const normalized = value.trim();
  return Object.values(DEFAULT_POST_PROCESSING_PROMPTS).some((prompt) => prompt.trim() === normalized);
}

export function formatCurrency(value: number, currency: "EUR" | "USD", localeTag: string, fractionDigits = 2): string {
  return new Intl.NumberFormat(localeTag, {
    style: "currency",
    currency,
    minimumFractionDigits: fractionDigits,
    maximumFractionDigits: fractionDigits,
  }).format(value);
}

export function formatEstimatedEuroFromUsd(usdValue: number, localeTag: string, fractionDigits = 2): string {
  return formatCurrency(usdValue * USD_TO_EUR_FOR_ESTIMATES, "EUR", localeTag, fractionDigits);
}

export function fixedEstimateExchangeRateLabel(localeTag: string): string {
  return `${formatCurrency(1, "USD", localeTag, 0)} = ${formatCurrency(USD_TO_EUR_FOR_ESTIMATES, "EUR", localeTag, 3)}`;
}

export function isSettingsSectionKey(value: string): value is SettingsSectionKey {
  return SETTINGS_SECTION_KEYS.some((key) => key === value);
}

export function chooseActiveSettingsSectionAtOffset(
  sectionTops: ReadonlyMap<SettingsSectionKey, number>,
  currentSection: SettingsSectionKey,
  activationOffset: number,
): SettingsSectionKey {
  const positionedSections = SETTINGS_SECTION_KEYS.flatMap((section) => {
    const top = sectionTops.get(section);
    return Number.isFinite(top) ? [{ section, top: top as number }] : [];
  });
  if (positionedSections.length === 0) {
    return currentSection;
  }

  const activationLine = activationOffset + 1;
  const reachedSections = positionedSections.filter(({ top }) => top <= activationLine);
  const activeTop =
    reachedSections.length > 0
      ? Math.max(...reachedSections.map(({ top }) => top))
      : Math.min(...positionedSections.map(({ top }) => top));
  const activeRow = positionedSections
    .filter(({ top }) => Math.abs(top - activeTop) <= 1)
    .map(({ section }) => section);

  return activeRow.includes(currentSection) ? currentSection : (activeRow[0] ?? currentSection);
}
