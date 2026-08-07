import { act, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const desktopThemeBoundary = vi.hoisted(() => ({
  runtime: { enabled: false },
  setWindowTheme: vi.fn(async (_theme: "dark" | "light") => undefined),
  setAppTheme: vi.fn(async (_theme: "dark" | "light") => undefined),
  invoke: vi.fn(async (_command: string, _payload: unknown) => undefined),
}));

vi.mock("@/lib/backend", () => ({
  isTauriRuntime: () => desktopThemeBoundary.runtime.enabled,
}));

vi.mock("@tauri-apps/api/window", () => ({
  getCurrentWindow: () => ({ setTheme: desktopThemeBoundary.setWindowTheme }),
}));

vi.mock("@tauri-apps/api/app", () => ({
  setTheme: desktopThemeBoundary.setAppTheme,
}));

vi.mock("@tauri-apps/api/core", () => ({
  invoke: desktopThemeBoundary.invoke,
}));

import { ThemeProvider, useTheme } from "./theme-provider";

type ResolvedTheme = "dark" | "light";

type Deferred = {
  promise: Promise<void>;
  resolve: () => void;
  reject: (reason?: unknown) => void;
};

type ControlledViewTransition = {
  ready: Deferred;
  finished: Deferred;
};

const originalInnerWidth = window.innerWidth;
const originalInnerHeight = window.innerHeight;

function deferred(): Deferred {
  let resolve!: () => void;
  let reject!: (reason?: unknown) => void;
  const promise = new Promise<void>((resolvePromise, rejectPromise) => {
    resolve = resolvePromise;
    reject = rejectPromise;
  });
  return { promise, resolve, reject };
}

function installControlledViewTransitions() {
  const transitions: ControlledViewTransition[] = [];
  const startViewTransition = vi.fn((updateCallback: () => void) => {
    const transition = { ready: deferred(), finished: deferred() };
    transitions.push(transition);
    updateCallback();
    return {
      ready: transition.ready.promise,
      finished: transition.finished.promise,
    };
  });

  Object.defineProperty(document, "startViewTransition", {
    configurable: true,
    value: startViewTransition,
  });

  return { startViewTransition, transitions };
}

function installAnimateSpy() {
  const animation = { cancel: vi.fn() } as unknown as Animation;
  const animate = vi.fn(
    (_keyframes: Keyframe[] | PropertyIndexedKeyframes, _options?: number | KeyframeAnimationOptions) => animation,
  );
  Object.defineProperty(document.documentElement, "animate", {
    configurable: true,
    value: animate,
  });
  return animate;
}

function ThemeControl({ target }: { target: ResolvedTheme }) {
  const { resolvedTheme, setTheme } = useTheme();
  return (
    <>
      <output data-testid="resolved-theme">{resolvedTheme}</output>
      <button
        type="button"
        onClick={() =>
          setTheme(target, {
            origin: { x: 100, y: 100 },
          })
        }
      >
        Switch to {target}
      </button>
    </>
  );
}

function renderThemeControl(source: ResolvedTheme, target: ResolvedTheme) {
  const storageKey = `theme-provider-test-${source}-to-${target}`;
  window.localStorage.setItem(storageKey, source);
  render(
    <ThemeProvider defaultTheme={source} storageKey={storageKey}>
      <ThemeControl target={target} />
    </ThemeProvider>,
  );
  expect(screen.getByTestId("resolved-theme")).toHaveTextContent(source);
  expect(document.documentElement).toHaveClass(source);
  return storageKey;
}

async function flushMicrotasks(rounds = 12) {
  for (let round = 0; round < rounds; round += 1) {
    await Promise.resolve();
  }
}

describe("ThemeProvider circular reveal", () => {
  beforeEach(() => {
    desktopThemeBoundary.runtime.enabled = false;
    desktopThemeBoundary.setWindowTheme.mockClear();
    desktopThemeBoundary.setAppTheme.mockClear();
    desktopThemeBoundary.invoke.mockClear();
    window.localStorage.clear();
    document.documentElement.classList.remove("dark", "light");
    document.documentElement.style.colorScheme = "";
    delete document.documentElement.dataset.themeRevealActive;
    document.querySelectorAll(".theme-reveal-overlay").forEach((overlay) => overlay.remove());
    vi.stubGlobal(
      "matchMedia",
      vi.fn((query: string) => ({
        matches: false,
        media: query,
        onchange: null,
        addEventListener: vi.fn(),
        removeEventListener: vi.fn(),
        addListener: vi.fn(),
        removeListener: vi.fn(),
        dispatchEvent: vi.fn(() => true),
      })),
    );
    Object.defineProperty(window, "innerWidth", { configurable: true, value: 500 });
    Object.defineProperty(window, "innerHeight", { configurable: true, value: 400 });
  });

  afterEach(() => {
    vi.useRealTimers();
    vi.unstubAllGlobals();
    Reflect.deleteProperty(document, "startViewTransition");
    Reflect.deleteProperty(document.documentElement, "animate");
    Object.defineProperty(window, "innerWidth", { configurable: true, value: originalInnerWidth });
    Object.defineProperty(window, "innerHeight", { configurable: true, value: originalInnerHeight });
    document.documentElement.classList.remove("dark", "light");
    document.documentElement.style.colorScheme = "";
    delete document.documentElement.dataset.themeRevealActive;
  });

  it.each([
    ["light", "dark"],
    ["dark", "light"],
  ] as const)("reveals %s to %s beyond every viewport corner", async (source, target) => {
    const { startViewTransition, transitions } = installControlledViewTransitions();
    const animate = installAnimateSpy();
    const storageKey = renderThemeControl(source, target);

    fireEvent.click(screen.getByRole("button", { name: `Switch to ${target}` }));

    expect(startViewTransition).toHaveBeenCalledOnce();
    expect(document.documentElement).toHaveClass(target);
    expect(document.documentElement).not.toHaveClass(source);
    expect(document.documentElement.style.colorScheme).toBe(target);
    expect(window.localStorage.getItem(storageKey)).toBe(target);
    expect(document.documentElement).toHaveAttribute("data-theme-reveal-active", "true");
    expect(animate).not.toHaveBeenCalled();

    await act(async () => {
      transitions[0].ready.resolve();
      await flushMicrotasks();
    });

    expect(animate).toHaveBeenCalledOnce();
    const [keyframes, options] = animate.mock.calls[0];
    const clipPath = (keyframes as PropertyIndexedKeyframes).clipPath;
    expect(clipPath).toEqual(expect.arrayContaining(["circle(0px at 100px 100px)"]));
    const finalClipPath = Array.isArray(clipPath) ? String(clipPath.at(-1)) : String(clipPath);
    const radius = Number(finalClipPath.match(/^circle\(([\d.]+)px at 100px 100px\)$/)?.[1]);

    // The worked viewport has a 3-4-5 far corner: (500 - 100, 400 - 100)
    // is (400, 300), so 500 px reaches the mathematical corner exactly. At
    // least two extra CSS pixels keep antialiasing/rounding out of the frame.
    expect(radius).toBeGreaterThanOrEqual(502);
    expect((options as KeyframeAnimationOptions & { pseudoElement?: string }).pseudoElement).toBe(
      "::view-transition-new(root)",
    );

    await act(async () => {
      transitions[0].finished.resolve();
      await flushMicrotasks();
    });
    expect(document.documentElement).not.toHaveAttribute("data-theme-reveal-active");
  });

  it.each([
    ["light", "dark"],
    ["dark", "light"],
  ] as const)("finishes %s to %s once without changing desktop chrome early", async (source, target) => {
    vi.useFakeTimers();
    const { transitions } = installControlledViewTransitions();
    installAnimateSpy();
    renderThemeControl(source, target);
    desktopThemeBoundary.runtime.enabled = true;

    fireEvent.click(screen.getByRole("button", { name: `Switch to ${target}` }));
    expect(document.documentElement).toHaveAttribute("data-theme-reveal-active", "true");
    expect(desktopThemeBoundary.setWindowTheme).not.toHaveBeenCalled();
    expect(desktopThemeBoundary.setAppTheme).not.toHaveBeenCalled();
    expect(desktopThemeBoundary.invoke).not.toHaveBeenCalled();

    act(() => vi.advanceTimersByTime(899));
    expect(desktopThemeBoundary.setWindowTheme).not.toHaveBeenCalled();
    expect(desktopThemeBoundary.setAppTheme).not.toHaveBeenCalled();
    expect(desktopThemeBoundary.invoke).not.toHaveBeenCalled();

    await act(async () => {
      transitions[0].finished.resolve();
      await flushMicrotasks();
    });

    expect(document.documentElement).not.toHaveAttribute("data-theme-reveal-active");
    expect(desktopThemeBoundary.setWindowTheme).toHaveBeenCalledOnce();
    expect(desktopThemeBoundary.setWindowTheme).toHaveBeenCalledWith(target);
    expect(desktopThemeBoundary.setAppTheme).toHaveBeenCalledOnce();
    expect(desktopThemeBoundary.setAppTheme).toHaveBeenCalledWith(target);
    expect(desktopThemeBoundary.invoke).toHaveBeenCalledOnce();
    expect(desktopThemeBoundary.invoke).toHaveBeenCalledWith("set_desktop_window_chrome_theme", { theme: target });

    await act(async () => {
      vi.advanceTimersByTime(1);
      await flushMicrotasks();
    });

    expect(desktopThemeBoundary.setWindowTheme).toHaveBeenCalledOnce();
    expect(desktopThemeBoundary.setAppTheme).toHaveBeenCalledOnce();
    expect(desktopThemeBoundary.invoke).toHaveBeenCalledOnce();
  });
});
