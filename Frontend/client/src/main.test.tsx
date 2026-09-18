import type { ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const boot = vi.hoisted(() => ({
  locale: Promise.resolve(),
  windowModule: Promise.resolve(),
  localeReady: false,
  imports: [] as string[],
  render: vi.fn(),
  createRoot: vi.fn(),
  observe: vi.fn(() => vi.fn()),
}));

vi.mock("react-dom/client", () => ({ createRoot: boot.createRoot }));
vi.mock("@fontsource/inter/400.css", () => ({}));
vi.mock("@carrot-kpi/switzer-font/latin-400.css", () => ({}));
vi.mock("./index.css", () => ({}));
vi.mock("./interface-polish.css", () => ({}));
vi.mock("./i18n", () => ({
  initializeLocaleCatalog: () => boot.locale,
  LocaleProvider: ({ children }: { children: ReactNode }) => children,
}));
vi.mock("./components/AppErrorBoundary", () => ({
  AppErrorBoundary: ({ children }: { children: ReactNode }) => children,
}));
vi.mock("./lib/frontend-performance", () => ({ startFrontendLongTaskObserver: boot.observe }));

function deferred() {
  let resolve!: () => void;
  let reject!: (error: Error) => void;
  const promise = new Promise<void>((done, fail) => {
    resolve = done;
    reject = fail;
  });
  return { promise, resolve, reject };
}

beforeEach(() => {
  vi.resetModules();
  boot.imports = [];
  boot.localeReady = false;
  boot.render.mockReset();
  boot.createRoot.mockReset().mockReturnValue({ render: boot.render });
  boot.observe.mockClear();
  document.body.innerHTML = '<div id="root"></div>';
  delete document.body.dataset.scriberOverlayWindow;
  delete document.documentElement.dataset.scriberOverlayWindow;
  vi.doMock("./App", async () => {
    boot.imports.push("main");
    await boot.windowModule;
    return { default: () => null };
  });
  vi.doMock("./components/TrayPanel", async () => {
    boot.imports.push("tray");
    await boot.windowModule;
    return { default: () => null };
  });
  vi.doMock("./components/NativeRecordingOverlay", async () => {
    boot.imports.push("overlay");
    await boot.windowModule;
    return { default: () => null };
  });
});

afterEach(() => {
  window.history.replaceState(null, "", "/");
});

describe("localized window bootstrap", () => {
  it.each([
    { query: "", expected: "main" },
    { query: "?tray=1", expected: "tray" },
    { query: "?overlay=1", expected: "overlay" },
  ])("loads only $expected alongside its locale and waits for both before rendering", async ({ query, expected }) => {
    const locale = deferred();
    const module = deferred();
    boot.locale = locale.promise.then(() => {
      boot.localeReady = true;
    });
    boot.windowModule = module.promise;
    window.history.replaceState(null, "", `/${query}`);
    await import("./main");

    await vi.waitFor(() => expect(boot.imports).toEqual([expected]));
    expect(boot.createRoot).not.toHaveBeenCalled();
    expect(boot.localeReady).toBe(false);
    expect(boot.observe).toHaveBeenCalledTimes(expected === "main" ? 1 : 0);
    if (expected === "overlay") {
      expect(document.documentElement.dataset.scriberOverlayWindow).toBe("true");
      expect(document.body.dataset.scriberOverlayWindow).toBe("true");
    }

    module.resolve();
    await Promise.resolve();
    expect(boot.render).not.toHaveBeenCalled();
    locale.resolve();
    await vi.waitFor(() => expect(boot.render).toHaveBeenCalledOnce());
    expect(boot.localeReady).toBe(true);
    expect(boot.observe).toHaveBeenCalledTimes(expected === "main" ? 1 : 0);
  });

  it("keeps the English fast path waiting for its window module", async () => {
    const module = deferred();
    boot.locale = Promise.resolve();
    boot.windowModule = module.promise;
    await import("./main");
    await vi.waitFor(() => expect(boot.imports).toEqual(["main"]));
    expect(boot.render).not.toHaveBeenCalled();
    module.resolve();
    await vi.waitFor(() => expect(boot.render).toHaveBeenCalledOnce());
  });

  it("retains startup fallback when the locale catalog fails", async () => {
    const locale = deferred();
    boot.locale = locale.promise;
    boot.windowModule = Promise.resolve();
    vi.spyOn(console, "debug").mockImplementation(() => undefined);
    await import("./main");
    await vi.waitFor(() => expect(boot.imports).toEqual(["main"]));
    locale.reject(new Error("catalog unavailable"));
    await vi.waitFor(() => expect(boot.render).toHaveBeenCalledOnce());
  });
});
