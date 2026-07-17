import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
  type ReactNode,
} from "react";
import { germanTranslations } from "@/i18n/translations/de";
import type { AppLocale, TranslationValues } from "@/i18n/types";

export type { AppLocale, TranslationValues } from "@/i18n/types";

export const LANGUAGE_STORAGE_KEY = "scriber-ui-locale";

const LOCALE_TAGS: Record<AppLocale, string> = {
  de: "de-DE",
  en: "en-US",
};

function isAppLocale(value: unknown): value is AppLocale {
  return value === "de" || value === "en";
}

function readStoredLocale(): AppLocale | null {
  if (typeof window === "undefined") {
    return null;
  }
  try {
    const stored = window.localStorage.getItem(LANGUAGE_STORAGE_KEY);
    return isAppLocale(stored) ? stored : null;
  } catch {
    return null;
  }
}

function writeStoredLocale(locale: AppLocale): void {
  if (typeof window === "undefined") {
    return;
  }
  try {
    window.localStorage.setItem(LANGUAGE_STORAGE_KEY, locale);
  } catch {
    // The active locale still applies for this session when storage is unavailable.
  }
}

async function syncDesktopLocale(locale: AppLocale): Promise<void> {
  if (typeof window === "undefined" || !("__TAURI_INTERNALS__" in window)) {
    return;
  }
  try {
    const { invoke } = await import("@tauri-apps/api/core");
    await invoke("set_ui_locale", { locale });
  } catch (error) {
    console.debug("Desktop interface locale update failed.", error);
  }
}

export function localeFromLanguages(languages: readonly string[]): AppLocale {
  for (const language of languages) {
    const normalized = language.toLowerCase();
    if (normalized.startsWith("de")) return "de";
    if (normalized.startsWith("en")) return "en";
  }
  return "en";
}

function preferredLocale(): AppLocale {
  if (typeof window === "undefined") {
    return "de";
  }
  const stored = readStoredLocale();
  if (stored) {
    return stored;
  }
  const browserLanguages = navigator.languages?.length
    ? navigator.languages
    : [navigator.language];
  return localeFromLanguages(browserLanguages);
}

let currentLocale: AppLocale = preferredLocale();

function interpolate(template: string, values?: TranslationValues): string {
  if (!values) {
    return template;
  }
  return template.replace(/\{\{([a-zA-Z0-9_]+)\}\}/g, (match, key: string) => {
    const value = values[key];
    return value === undefined ? match : String(value);
  });
}

export function translate(
  locale: AppLocale,
  source: string,
  values?: TranslationValues,
): string {
  const template = locale === "de" && Object.prototype.hasOwnProperty.call(germanTranslations, source)
    ? germanTranslations[source]
    : source;
  return interpolate(template, values);
}

export function localizeLegacyDateLabel(locale: AppLocale, value: string): string {
  const source = String(value || "").trim();
  const todayMatch = /^Today,\s*(.+)$/.exec(source);
  if (todayMatch) {
    return translate(locale, "Today at {{time}}", { time: todayMatch[1] });
  }
  const isoDateMatch = /^(\d{4})-(\d{2})-(\d{2})$/.exec(source);
  if (isoDateMatch) {
    const year = Number(isoDateMatch[1]);
    const month = Number(isoDateMatch[2]);
    const day = Number(isoDateMatch[3]);
    const date = new Date(year, month - 1, day);
    if (
      date.getFullYear() === year
      && date.getMonth() === month - 1
      && date.getDate() === day
    ) {
      return new Intl.DateTimeFormat(LOCALE_TAGS[locale], { dateStyle: "medium" }).format(date);
    }
  }
  return translate(locale, source);
}

export function translateNow(source: string, values?: TranslationValues): string {
  return translate(currentLocale, source, values);
}

export function getCurrentLocale(): AppLocale {
  return currentLocale;
}

export function getLocaleTag(locale: AppLocale = currentLocale): string {
  return LOCALE_TAGS[locale];
}

export function formatNumberNow(value: number, options?: Intl.NumberFormatOptions): string {
  return new Intl.NumberFormat(getLocaleTag(), options).format(value);
}

interface I18nContextValue {
  locale: AppLocale;
  localeTag: string;
  setLocale: (locale: AppLocale) => void;
  toggleLocale: () => void;
  t: (source: string, values?: TranslationValues) => string;
  formatDate: (value: Date | number | string, options?: Intl.DateTimeFormatOptions) => string;
  formatLegacyDate: (value: string) => string;
  formatNumber: (value: number, options?: Intl.NumberFormatOptions) => string;
}

const I18nContext = createContext<I18nContextValue | null>(null);

export function LocaleProvider({ children }: { children: ReactNode }) {
  const [locale, setLocaleState] = useState<AppLocale>(() => preferredLocale());

  const setLocale = useCallback((nextLocale: AppLocale) => {
    currentLocale = nextLocale;
    setLocaleState(nextLocale);
    writeStoredLocale(nextLocale);
  }, []);

  const toggleLocale = useCallback(() => {
    setLocale(locale === "de" ? "en" : "de");
  }, [locale, setLocale]);

  useEffect(() => {
    currentLocale = locale;
    document.documentElement.lang = locale;
    void syncDesktopLocale(locale);
  }, [locale]);

  useEffect(() => {
    const handleStorage = (event: StorageEvent) => {
      if (event.key === LANGUAGE_STORAGE_KEY && isAppLocale(event.newValue)) {
        currentLocale = event.newValue;
        setLocaleState(event.newValue);
      }
    };
    window.addEventListener("storage", handleStorage);
    return () => window.removeEventListener("storage", handleStorage);
  }, []);

  const t = useCallback(
    (source: string, values?: TranslationValues) => translate(locale, source, values),
    [locale],
  );

  const formatDate = useCallback(
    (value: Date | number | string, options?: Intl.DateTimeFormatOptions) => {
      const date = value instanceof Date ? value : new Date(value);
      return Number.isNaN(date.getTime())
        ? ""
        : new Intl.DateTimeFormat(LOCALE_TAGS[locale], options).format(date);
    },
    [locale],
  );

  const formatLegacyDate = useCallback(
    (value: string) => localizeLegacyDateLabel(locale, value),
    [locale],
  );

  const formatNumber = useCallback(
    (value: number, options?: Intl.NumberFormatOptions) =>
      new Intl.NumberFormat(LOCALE_TAGS[locale], options).format(value),
    [locale],
  );

  const contextValue = useMemo<I18nContextValue>(() => ({
    locale,
    localeTag: LOCALE_TAGS[locale],
    setLocale,
    toggleLocale,
    t,
    formatDate,
    formatLegacyDate,
    formatNumber,
  }), [formatDate, formatLegacyDate, formatNumber, locale, setLocale, t, toggleLocale]);

  return <I18nContext.Provider value={contextValue}>{children}</I18nContext.Provider>;
}

export function useI18n(): I18nContextValue {
  const context = useContext(I18nContext);
  if (!context) {
    throw new Error("useI18n must be used within LocaleProvider");
  }
  return context;
}
