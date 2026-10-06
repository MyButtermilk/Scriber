import { useRef } from "react";
import { act, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { LocaleProvider } from "@/i18n";
import type { SummaryOutlineItem } from "@/lib/summary-html";
import { SummaryTableOfContents } from "./transcript-summary-document";

const frames = new Map<number, FrameRequestCallback>();
let frameId = 0;
let headingReads = 0;
let layoutShift = 0;

function rect(top: number, height: number): DOMRect {
  return { x: 0, y: top, top, left: 0, bottom: top + height, right: 900, width: 900, height, toJSON: () => ({}) };
}

beforeEach(() => {
  frames.clear();
  headingReads = 0;
  layoutShift = 0;
  vi.spyOn(window, "requestAnimationFrame").mockImplementation((callback) => {
    frames.set(++frameId, callback);
    return frameId;
  });
  vi.spyOn(window, "cancelAnimationFrame").mockImplementation((id) => {
    frames.delete(id);
  });
  vi.stubGlobal("IntersectionObserver", undefined);
  vi.stubGlobal("ResizeObserver", undefined);
  vi.spyOn(HTMLElement.prototype, "clientHeight", "get").mockReturnValue(600);
  vi.spyOn(HTMLElement.prototype, "scrollHeight", "get").mockReturnValue(103_000);
  vi.spyOn(HTMLElement.prototype, "getBoundingClientRect").mockImplementation(function (this: HTMLElement) {
    if (this.tagName === "LI") return rect(100, 30);
    if (this.dataset.headingIndex !== undefined) {
      headingReads++;
      const root = document.querySelector<HTMLElement>("[data-testid=summary-scroll]")!;
      return rect(50 + Number(this.dataset.headingIndex) * 200 + layoutShift - root.scrollTop, 30);
    }
    return rect(50, 600);
  });
});
afterEach(() => vi.unstubAllGlobals());

function flushFrames() {
  act(() => {
    for (let pass = 0; frames.size && pass < 10; pass++) {
      const pending = Array.from(frames.values());
      frames.clear();
      pending.forEach((callback) => callback(0));
    }
  });
}

function Document({
  outline,
  missing = "",
  table = false,
}: {
  outline: SummaryOutlineItem[];
  missing?: string;
  table?: boolean;
}) {
  const scrollContainerRef = useRef<HTMLDivElement>(null);
  const headings = outline.map((item, index) =>
    item.id === missing ? null : (
      <h2 key={item.id} id={item.id} data-heading-index={index}>
        {item.label}
      </h2>
    ),
  );
  return (
    <LocaleProvider>
      <div ref={scrollContainerRef} data-testid="summary-scroll">
        {table ? (
          <table>
            <tbody>
              <tr>
                {headings.map((heading, index) => (
                  <td key={index}>{heading}</td>
                ))}
              </tr>
            </tbody>
          </table>
        ) : (
          headings
        )}
      </div>
      <SummaryTableOfContents outline={outline} scrollContainerRef={scrollContainerRef} title="Contents" />
    </LocaleProvider>
  );
}

const outline: SummaryOutlineItem[] = Array.from({ length: 512 }, (_, index) => ({
  id: `section-${index}`,
  label: `Section ${index}`,
  level: 2,
}));

function scroll(top: number) {
  const root = screen.getByTestId("summary-scroll");
  root.scrollTop = top;
  fireEvent.scroll(root);
  flushFrames();
}

it("finds the active heading with logarithmic layout reads after arbitrary scrolls", () => {
  render(<Document outline={outline} />);
  flushFrames();
  const lookup = vi.spyOn(document, "getElementById");
  for (const index of [300, 5, 500, 0, 210]) {
    headingReads = 0;
    scroll(index * 200);
    expect(screen.getByRole("link", { name: `Section ${index}` })).toHaveAttribute("aria-current", "location");
    expect(headingReads).toBeLessThanOrEqual(10);
  }
  expect(lookup).not.toHaveBeenCalled();
});

it("uses fresh geometry after layout shifts, handles missing headings and the bottom", () => {
  render(<Document outline={outline} missing="section-300" />);
  flushFrames();
  scroll(60_000);
  expect(screen.getByRole("link", { name: "Section 299" })).toHaveAttribute("aria-current", "location");
  layoutShift = 400;
  scroll(60_000);
  expect(screen.getByRole("link", { name: "Section 298" })).toHaveAttribute("aria-current", "location");
  scroll(102_400);
  expect(screen.getByRole("link", { name: "Section 511" })).toHaveAttribute("aria-current", "location");
});

it("refreshes heading references when a replacement summary arrives", () => {
  const view = render(<Document outline={outline} />);
  flushFrames();
  const replacement = outline
    .slice(0, 3)
    .map((item) => ({ ...item, id: `new-${item.id}`, label: `New ${item.label}` }));
  view.rerender(<Document outline={replacement} />);
  flushFrames();
  scroll(200);
  expect(screen.getByRole("link", { name: "New Section 1" })).toHaveAttribute("aria-current", "location");
});

it("preserves document-order selection for headings in table columns", () => {
  render(<Document outline={outline.slice(0, 3)} table />);
  document.getElementById("section-1")!.dataset.headingIndex = "2";
  document.getElementById("section-2")!.dataset.headingIndex = "1";
  flushFrames();
  scroll(200);
  expect(screen.getByRole("link", { name: "Section 2" })).toHaveAttribute("aria-current", "location");
});

it("keeps the last tied heading and honors an explicit navigation target", () => {
  render(<Document outline={outline.slice(0, 3)} />);
  document.getElementById("section-1")!.dataset.headingIndex = "0";
  flushFrames();
  expect(screen.getByRole("link", { name: "Section 1" })).toHaveAttribute("aria-current", "location");
  const root = screen.getByTestId("summary-scroll");
  root.scrollTo = vi.fn();
  fireEvent.click(screen.getByRole("link", { name: "Section 2" }));
  expect(root.scrollTo).toHaveBeenCalledWith({ behavior: "smooth", top: 400 });
  scroll(0);
  expect(screen.getByRole("link", { name: "Section 2" })).toHaveAttribute("aria-current", "location");
  window.history.replaceState(null, "", window.location.pathname);
});
