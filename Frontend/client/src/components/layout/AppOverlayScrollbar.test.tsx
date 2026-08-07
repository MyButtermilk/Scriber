import { useRef } from "react";
import { act, fireEvent, render } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { AppOverlayScrollbar } from "./AppOverlayScrollbar";

let resizeObserverCallback: ResizeObserverCallback | null = null;

class ResizeObserverMock {
  constructor(callback: ResizeObserverCallback) {
    resizeObserverCallback = callback;
  }

  observe = vi.fn();
  disconnect = vi.fn();
  unobserve = vi.fn();
}

function Harness() {
  const scrollContainerRef = useRef<HTMLDivElement>(null);
  return (
    <div>
      <div ref={scrollContainerRef} data-testid="viewport">
        <button type="button">Focusable content</button>
      </div>
      <AppOverlayScrollbar scrollContainerRef={scrollContainerRef} />
    </div>
  );
}

function setDimensions(element: HTMLElement, dimensions: { clientHeight: number; scrollHeight?: number }) {
  Object.defineProperty(element, "clientHeight", {
    configurable: true,
    value: dimensions.clientHeight,
  });
  if (dimensions.scrollHeight !== undefined) {
    Object.defineProperty(element, "scrollHeight", {
      configurable: true,
      value: dimensions.scrollHeight,
    });
  }
}

function runResizeObserver() {
  act(() => {
    resizeObserverCallback?.([], {} as ResizeObserver);
    vi.advanceTimersByTime(0);
  });
}

describe("AppOverlayScrollbar", () => {
  beforeEach(() => {
    vi.useFakeTimers();
    resizeObserverCallback = null;
    vi.stubGlobal("ResizeObserver", ResizeObserverMock);
    vi.stubGlobal("requestAnimationFrame", (callback: FrameRequestCallback) =>
      window.setTimeout(() => callback(performance.now()), 0),
    );
    vi.stubGlobal("cancelAnimationFrame", (frameId: number) => window.clearTimeout(frameId));
  });

  afterEach(() => {
    vi.useRealTimers();
    vi.unstubAllGlobals();
  });

  it("stays absent at rest and maps the viewport position onto an inset thumb", () => {
    const { container, getByTestId } = render(<Harness />);
    const viewport = getByTestId("viewport");
    const scrollbar = container.querySelector<HTMLElement>("[data-app-overlay-scrollbar]");
    const thumb = container.querySelector<HTMLElement>(".app-overlay-scrollbar-thumb");
    expect(scrollbar).not.toBeNull();
    expect(thumb).not.toBeNull();

    setDimensions(viewport, { clientHeight: 400, scrollHeight: 1_000 });
    setDimensions(scrollbar!, { clientHeight: 388 });
    runResizeObserver();

    expect(scrollbar).toHaveAttribute("data-scrollable", "true");
    expect(scrollbar).toHaveAttribute("data-visible", "false");
    expect(thumb?.style.height).toBe("155.2px");
    expect(thumb?.style.transform).toBe("translate3d(0, 0px, 0)");

    viewport.scrollTop = 300;
    fireEvent.scroll(viewport);
    act(() => vi.advanceTimersByTime(0));

    expect(scrollbar).toHaveAttribute("data-visible", "true");
    expect(thumb?.style.transform).toBe("translate3d(0, 116.4px, 0)");
  });

  it("hides 700 ms after scroll, pointer, or keyboard-focus activity and resets the idle timer", () => {
    const { container, getByRole, getByTestId } = render(<Harness />);
    const viewport = getByTestId("viewport");
    const scrollbar = container.querySelector<HTMLElement>("[data-app-overlay-scrollbar]");
    setDimensions(viewport, { clientHeight: 400, scrollHeight: 1_000 });
    setDimensions(scrollbar!, { clientHeight: 388 });
    runResizeObserver();

    fireEvent.scroll(viewport);
    expect(scrollbar).toHaveAttribute("data-visible", "true");
    act(() => vi.advanceTimersByTime(699));
    expect(scrollbar).toHaveAttribute("data-visible", "true");

    fireEvent.pointerMove(viewport);
    act(() => vi.advanceTimersByTime(699));
    expect(scrollbar).toHaveAttribute("data-visible", "true");
    act(() => vi.advanceTimersByTime(1));
    expect(scrollbar).toHaveAttribute("data-visible", "false");

    fireEvent.focusIn(getByRole("button", { name: "Focusable content" }));
    expect(scrollbar).toHaveAttribute("data-visible", "true");
    act(() => vi.advanceTimersByTime(700));
    expect(scrollbar).toHaveAttribute("data-visible", "false");
  });

  it("keeps the thumb visible while it is dragged and scrolls in proportion to pointer travel", () => {
    const { container, getByTestId } = render(<Harness />);
    const viewport = getByTestId("viewport");
    const scrollbar = container.querySelector<HTMLElement>("[data-app-overlay-scrollbar]");
    const thumb = container.querySelector<HTMLElement>(".app-overlay-scrollbar-thumb");
    setDimensions(viewport, { clientHeight: 400, scrollHeight: 1_000 });
    setDimensions(scrollbar!, { clientHeight: 388 });
    runResizeObserver();

    fireEvent.pointerEnter(viewport);
    fireEvent.pointerDown(thumb!, { clientY: 10, pointerId: 7 });
    expect(scrollbar).toHaveAttribute("data-dragging", "true");
    expect(scrollbar).toHaveAttribute("data-visible", "true");

    fireEvent.pointerMove(thumb!, { clientY: 126.4, pointerId: 7 });
    act(() => vi.advanceTimersByTime(0));
    expect(viewport.scrollTop).toBeCloseTo(300);

    fireEvent.pointerUp(thumb!, { clientY: 126.4, pointerId: 7 });
    expect(scrollbar).toHaveAttribute("data-dragging", "false");
    act(() => vi.advanceTimersByTime(700));
    expect(scrollbar).toHaveAttribute("data-visible", "false");
  });
});
