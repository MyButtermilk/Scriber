// Cache formatter instances, never formatted user data. Options in our UI are
// small plain records; unusual/prototype/getter-based options retain native
// Intl behavior by bypassing the cache.
const MAX_FORMATTERS = 32;
const numbers = new Map<string, Intl.NumberFormat>();
const dates = new Map<string, Intl.DateTimeFormat>();

function optionsKey(locale: string, options?: object): string | null {
  if (options === undefined) return JSON.stringify([locale, {}]);
  if (
    options === null ||
    (Object.getPrototypeOf(options) !== Object.prototype && Object.getPrototypeOf(options) !== null)
  )
    return null;
  const entries = Object.entries(Object.getOwnPropertyDescriptors(options));
  if (
    entries.some(
      ([, descriptor]) =>
        !("value" in descriptor) ||
        (descriptor.value !== undefined &&
          typeof descriptor.value !== "string" &&
          typeof descriptor.value !== "boolean" &&
          !(typeof descriptor.value === "number" && Number.isFinite(descriptor.value))),
    )
  )
    return null;
  return JSON.stringify([
    locale,
    Object.fromEntries(
      entries.sort(([a], [b]) => a.localeCompare(b)).map(([key, descriptor]) => [key, descriptor.value]),
    ),
  ]);
}

function cached<T>(cache: Map<string, T>, key: string | null, create: () => T): T {
  if (key === null) return create();
  const previous = cache.get(key);
  if (previous) return previous;
  const formatter = create();
  if (cache.size >= MAX_FORMATTERS) cache.delete(cache.keys().next().value!);
  cache.set(key, formatter);
  return formatter;
}

export function numberFormatter(locale: string, options?: Intl.NumberFormatOptions): Intl.NumberFormat {
  return cached(numbers, optionsKey(locale, options), () => new Intl.NumberFormat(locale, options));
}

export function dateFormatter(locale: string, options?: Intl.DateTimeFormatOptions): Intl.DateTimeFormat {
  return cached(dates, optionsKey(locale, options), () => new Intl.DateTimeFormat(locale, options));
}

// Re-resolve the OS default time zone after the user returns to the app.
export function resetDateFormatters(): void {
  dates.clear();
}
