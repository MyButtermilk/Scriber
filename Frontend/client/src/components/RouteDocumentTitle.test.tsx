import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it } from "vitest";
import { Router } from "wouter";
import { memoryLocation } from "wouter/memory-location";

import { RouteDocumentTitle } from "./RouteDocumentTitle";
import { LANGUAGE_STORAGE_KEY, LocaleProvider, useI18n } from "@/i18n";
import { routeTitleSource } from "@/lib/route-titles";

function LocaleControl() {
  const { setLocale } = useI18n();
  return (
    <>
      <button type="button" onClick={() => setLocale("de")}>
        Deutsch
      </button>
      <button type="button" onClick={() => setLocale("en")}>
        English
      </button>
    </>
  );
}

describe("RouteDocumentTitle", () => {
  beforeEach(() => {
    document.title = "Scriber";
    window.localStorage.setItem(LANGUAGE_STORAGE_KEY, "en");
  });

  it.each([
    ["/", "Live Mic"],
    ["/meetings", "Meetings"],
    ["/meetings/42", "Meetings"],
    ["/youtube", "YouTube"],
    ["/file", "File"],
    ["/debug", "Console"],
    ["/settings", "Settings"],
    ["/transcript/42", "Transcript"],
    ["/missing", "Page not found"],
  ] as const)("maps %s to %s", (path, expected) => {
    expect(routeTitleSource(path)).toBe(expected);
  });

  it("updates the title after navigation and locale changes", async () => {
    const location = memoryLocation({ path: "/settings", record: true });

    render(
      <Router hook={location.hook}>
        <LocaleProvider>
          <RouteDocumentTitle />
          <LocaleControl />
        </LocaleProvider>
      </Router>,
    );

    await waitFor(() => expect(document.title).toBe("Settings | Scriber"));

    act(() => location.navigate("/transcript/42"));
    await waitFor(() => expect(document.title).toBe("Transcript | Scriber"));

    fireEvent.click(screen.getByRole("button", { name: "Deutsch" }));
    await waitFor(() => expect(document.title).toBe("Transkript | Scriber"));

    fireEvent.click(screen.getByRole("button", { name: "English" }));
    await waitFor(() => expect(document.title).toBe("Transcript | Scriber"));
  });
});
