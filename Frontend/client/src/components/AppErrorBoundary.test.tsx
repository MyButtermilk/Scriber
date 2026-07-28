import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { useEffect, type ReactNode } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { AppErrorBoundary } from "./AppErrorBoundary";
import { LANGUAGE_STORAGE_KEY, LocaleProvider, useI18n } from "@/i18n";

function GermanLocale({ children }: { children: ReactNode }) {
  const { setLocale } = useI18n();

  useEffect(() => {
    setLocale("de");
  }, [setLocale]);

  return children;
}

describe("AppErrorBoundary", () => {
  beforeEach(() => {
    window.localStorage.setItem(LANGUAGE_STORAGE_KEY, "en");
    vi.spyOn(console, "error").mockImplementation(() => undefined);
  });

  it("keeps the surrounding shell mounted and can retry a failed route", async () => {
    let shouldThrow = true;
    const navigateHome = vi.fn();

    function RouteContent() {
      if (shouldThrow) {
        throw new Error("route failed");
      }
      return <p>Recovered route</p>;
    }

    render(
      <LocaleProvider>
        <GermanLocale>
          <div data-testid="app-shell">
            <nav>Persistent navigation</nav>
            <AppErrorBoundary variant="route" resetKey="/settings" onNavigateHome={navigateHome}>
              <RouteContent />
            </AppErrorBoundary>
          </div>
        </GermanLocale>
      </LocaleProvider>,
    );

    expect(screen.getByTestId("app-shell")).toHaveTextContent("Persistent navigation");
    await waitFor(() => {
      expect(screen.getByRole("alert")).toHaveAccessibleName("Dieser Bereich konnte nicht geöffnet werden");
    });
    fireEvent.click(await screen.findByRole("button", { name: "Zurück zum Live-Mikrofon" }));
    expect(navigateHome).toHaveBeenCalledOnce();

    shouldThrow = false;
    fireEvent.click(screen.getByRole("button", { name: "Erneut versuchen" }));

    expect(screen.getByText("Recovered route")).toBeInTheDocument();
  });

  it("renders a localized root fallback", async () => {
    function BrokenApp(): ReactNode {
      throw new Error("root failed");
    }

    render(
      <LocaleProvider>
        <GermanLocale>
          <AppErrorBoundary variant="root">
            <BrokenApp />
          </AppErrorBoundary>
        </GermanLocale>
      </LocaleProvider>,
    );

    await waitFor(() => {
      expect(screen.getByRole("alert")).toHaveAccessibleName("Scriber konnte nicht gestartet werden");
    });
    expect(await screen.findByRole("button", { name: "Scriber neu laden" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Zurück zum Live-Mikrofon" })).not.toBeInTheDocument();
  });

  it("recovers automatically when navigation changes the route reset key", async () => {
    let shouldThrow = true;

    function RouteContent() {
      if (shouldThrow) {
        throw new Error("route failed");
      }
      return <p>Next route</p>;
    }

    const renderBoundary = (resetKey: string) => (
      <LocaleProvider>
        <AppErrorBoundary variant="route" resetKey={resetKey}>
          <RouteContent />
        </AppErrorBoundary>
      </LocaleProvider>
    );
    const view = render(renderBoundary("/settings"));

    expect(screen.getByRole("alert")).toBeInTheDocument();

    shouldThrow = false;
    view.rerender(renderBoundary("/youtube"));

    expect(await screen.findByText("Next route")).toBeInTheDocument();
  });
});
