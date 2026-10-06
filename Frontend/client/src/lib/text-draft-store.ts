import { useState, useSyncExternalStore } from "react";

/**
 * One editable text value with targeted subscriptions.
 *
 * Large forms keep frequently typed values here instead of in page state, so a
 * keystroke re-renders only the field displaying the draft. Event handlers read
 * the latest value with `get()`; loads, resets and canonicalization use `set()`
 * and reach the field exactly like typing does.
 */
export interface TextDraftStore {
  get(): string;
  set(value: string): void;
  subscribe(listener: () => void): () => void;
}

export function createTextDraftStore(initialValue = ""): TextDraftStore {
  let value = initialValue;
  const listeners = new Set<() => void>();
  return {
    get: () => value,
    set(nextValue) {
      if (nextValue === value) return;
      value = nextValue;
      // A listener may write again (for example a locale default) or
      // unsubscribe another field. Iterate a snapshot and skip removed ones.
      for (const listener of Array.from(listeners)) {
        if (listeners.has(listener)) listener();
      }
    },
    subscribe(listener) {
      listeners.add(listener);
      return () => {
        listeners.delete(listener);
      };
    },
  };
}

export function useTextDraftStore(initialValue: string | (() => string) = ""): TextDraftStore {
  const [store] = useState(() =>
    createTextDraftStore(typeof initialValue === "function" ? initialValue() : initialValue),
  );
  return store;
}

export function useTextDraft(store: TextDraftStore): string {
  return useSyncExternalStore(store.subscribe, store.get, store.get);
}
