export type AppLocale = "de" | "en";

export type TranslationValues = Record<string, string | number>;

export type TranslationCatalog = Readonly<Record<string, string>>;
