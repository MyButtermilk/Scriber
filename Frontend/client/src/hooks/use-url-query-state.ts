import { useCallback, useEffect, useRef, useState } from "react";

type ParseFn<T> = (raw: string | null) => T;
type SerializeFn<T> = (value: T) => string | null;

interface UseUrlQueryStateOptions<T> {
  parse?: ParseFn<T>;
  serialize?: SerializeFn<T>;
  syncDelayMs?: number;
}

function defaultSerialize<T>(value: T): string {
  return String(value);
}

export function useUrlQueryState<T>(
  key: string,
  defaultValue: T,
  options: UseUrlQueryStateOptions<T> = {},
) {
  const parseRef = useRef<ParseFn<T> | undefined>(options.parse);
  const serializeRef = useRef<SerializeFn<T>>(options.serialize ?? defaultSerialize);
  parseRef.current = options.parse;
  serializeRef.current = options.serialize ?? defaultSerialize;

  const readFromUrl = useCallback((): T => {
    if (typeof window === "undefined") {
      return defaultValue;
    }

    const raw = new URLSearchParams(window.location.search).get(key);
    if (parseRef.current) {
      return parseRef.current(raw);
    }
    if (raw === null) {
      return defaultValue;
    }
    return raw as unknown as T;
  }, [defaultValue, key]);

  const [value, setValue] = useState<T>(readFromUrl);
  const syncDelayMs = options.syncDelayMs ?? 0;

  useEffect(() => {
    if (typeof window === "undefined") {
      return;
    }
    const onPopState = () => {
      setValue(readFromUrl());
    };
    window.addEventListener("popstate", onPopState);
    return () => {
      window.removeEventListener("popstate", onPopState);
    };
  }, [readFromUrl]);

  useEffect(() => {
    if (typeof window === "undefined") {
      return;
    }

    const sync = () => {
      const params = new URLSearchParams(window.location.search);
      const serialized = serializeRef.current(value);
      const serializedDefault = serializeRef.current(defaultValue);

      if (!serialized || serialized === serializedDefault) {
        params.delete(key);
      } else {
        params.set(key, serialized);
      }

      const nextSearch = params.toString();
      const nextUrl = `${window.location.pathname}${nextSearch ? `?${nextSearch}` : ""}${window.location.hash}`;
      const currentUrl = `${window.location.pathname}${window.location.search}${window.location.hash}`;

      if (nextUrl !== currentUrl) {
        window.history.replaceState(window.history.state, "", nextUrl);
      }
    };

    if (syncDelayMs > 0) {
      const timer = window.setTimeout(sync, syncDelayMs);
      return () => {
        window.clearTimeout(timer);
      };
    }

    sync();
  }, [defaultValue, key, syncDelayMs, value]);

  return [value, setValue] as const;
}
