import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { LocaleProvider, LANGUAGE_STORAGE_KEY } from "@/i18n";
import { fetchWithTimeout } from "@/lib/fetch-with-timeout";
import DebugConsole from "./DebugConsole";

const observed = vi.hoisted(() => ({ keys: 0 }));
vi.mock("@/lib/fetch-with-timeout", () => ({ fetchWithTimeout: vi.fn() }));
// Keep the real virtualizer; count its key work without changing callback identity.
vi.mock("@tanstack/react-virtual", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@tanstack/react-virtual")>();
  const wrappers = new WeakMap<
    (index: number) => string | number | bigint,
    (index: number) => string | number | bigint
  >();
  return {
    ...actual,
    useVirtualizer: (options: Parameters<typeof actual.useVirtualizer>[0]) => {
      const original = options.getItemKey!;
      let wrapped = wrappers.get(original);
      if (!wrapped) {
        wrapped = (index) => {
          observed.keys++;
          return original(index);
        };
        wrappers.set(original, wrapped);
      }
      return actual.useVirtualizer({ ...options, getItemKey: wrapped });
    },
  };
});

beforeEach(() => {
  localStorage.setItem(LANGUAGE_STORAGE_KEY, "en");
  vi.stubGlobal(
    "ResizeObserver",
    class {
      observe() {}
      unobserve() {}
      disconnect() {}
    },
  );
  vi.spyOn(HTMLElement.prototype, "offsetHeight", "get").mockReturnValue(600);
  vi.spyOn(HTMLElement.prototype, "offsetWidth", "get").mockReturnValue(900);
  Object.defineProperty(HTMLElement.prototype, "scrollTo", { configurable: true, value: () => {} });
  vi.spyOn(HTMLElement.prototype, "getBoundingClientRect").mockReturnValue({
    x: 0,
    y: 0,
    top: 0,
    left: 0,
    bottom: 600,
    right: 900,
    width: 900,
    height: 600,
    toJSON: () => ({}),
  });
  const items = Array.from({ length: 1200 }, (_, line) => ({
    source: "runtime.log",
    line,
    timestampMs: Date.now(),
    level: "INFO",
    message: `Event ${line}`,
    context: { event: "test", meta: { count: line } },
  }));
  vi.mocked(fetchWithTimeout).mockImplementation(async (url) =>
    Response.json(
      String(url).includes("/logs?") ? { items, sources: ["runtime.log"], loggingEnabled: true } : { items: [] },
    ),
  );
});
afterEach(() => {
  Reflect.deleteProperty(HTMLElement.prototype, "scrollTo");
  vi.unstubAllGlobals();
});

it("reuses all-record keys when refreshing an unchanged snapshot and scrolling", async () => {
  const view = render(
    <LocaleProvider>
      <DebugConsole />
    </LocaleProvider>,
  );
  await screen.findByText("Event 1199");
  await waitFor(() => expect(screen.getByRole("button", { name: "Refresh logs" })).not.toBeDisabled());
  observed.keys = 0;
  await act(async () => {
    fireEvent.click(screen.getByRole("button", { name: "Refresh logs" }));
  });
  expect(observed.keys).toBeLessThan(100);
  observed.keys = 0;
  const scroller = view.container.querySelector<HTMLElement>(".debug-log-scroll")!;
  await act(async () => {
    scroller.scrollTop = 20_000;
    fireEvent.scroll(scroller);
  });
  expect(observed.keys).toBeLessThan(100);
  expect(view.container.querySelectorAll(".debug-log-row").length).toBeGreaterThan(0);
  expect(view.container.querySelectorAll(".debug-log-row").length).toBeLessThan(40);
  expect(screen.queryByText("Event 1199")).not.toBeInTheDocument();
});
