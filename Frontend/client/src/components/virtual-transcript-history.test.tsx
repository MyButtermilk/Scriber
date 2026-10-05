import { act, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { LocaleProvider } from "@/i18n";
import { VirtualTranscriptHistory } from "./virtual-transcript-history";

beforeEach(() => {
  vi.stubGlobal(
    "ResizeObserver",
    class {
      observe() {}
      unobserve() {}
      disconnect() {}
    },
  );
  vi.stubGlobal(
    "IntersectionObserver",
    class {
      observe() {}
      unobserve() {}
      disconnect() {}
    },
  );
  vi.spyOn(HTMLElement.prototype, "offsetHeight", "get").mockReturnValue(600);
  vi.spyOn(HTMLElement.prototype, "offsetWidth", "get").mockReturnValue(900);
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
});
afterEach(() => vi.unstubAllGlobals());

it("scrolling a long history reuses item keys and still reveals the correct rows", async () => {
  const items = Array.from({ length: 10_000 }, (_, id) => ({ id: `entry-${id}` }));
  const key = vi.fn((item: { id: string }) => item.id);
  const view = render(
    <LocaleProvider>
      <div data-app-scroll-container>
        <VirtualTranscriptHistory
          items={items}
          viewMode="list"
          getItemKey={key}
          renderItem={(item) => <span>{item.id}</span>}
        />
      </div>
    </LocaleProvider>,
  );
  const scroller = view.container.querySelector<HTMLElement>("[data-app-scroll-container]")!;
  expect(screen.getByText("entry-0")).toBeInTheDocument();
  key.mockClear();
  await act(async () => {
    scroller.scrollTop = 20_000;
    fireEvent.scroll(scroller);
  });
  expect(screen.queryByText("entry-0")).not.toBeInTheDocument();
  expect(screen.getAllByText(/^entry-/).length).toBeGreaterThan(0);
  // Visible rows still resolve their keys for DOM measurement. The complete
  // history must not be revisited (baseline: 10,054 calls for this scroll).
  expect(key.mock.calls.length).toBeLessThan(100);
  expect(view.container.querySelectorAll("span").length).toBeLessThan(40);
});

it("rebuilds history keys when a new result replaces the current list", () => {
  const key = (item: { id: string }) => item.id;
  const content = (id: string) => (
    <LocaleProvider>
      <div data-app-scroll-container>
        <VirtualTranscriptHistory
          items={[{ id }]}
          viewMode="list"
          getItemKey={key}
          renderItem={(item) => <span>{item.id}</span>}
        />
      </div>
    </LocaleProvider>
  );
  const view = render(content("old-result"));
  expect(screen.getByText("old-result")).toBeInTheDocument();
  view.rerender(content("new-result"));
  expect(screen.getByText("new-result")).toBeInTheDocument();
  expect(screen.queryByText("old-result")).not.toBeInTheDocument();
});
