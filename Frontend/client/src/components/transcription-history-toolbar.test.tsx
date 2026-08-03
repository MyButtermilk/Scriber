import { useState } from "react";
import { fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { TranscriptionHistoryToolbar } from "@/components/transcription-history-toolbar";
import type { TranscriptHistoryViewMode } from "@/hooks/use-transcript-history-panel-state";
import { LANGUAGE_STORAGE_KEY, LocaleProvider } from "@/i18n";

class ResizeObserverMock {
  observe = vi.fn();
  disconnect = vi.fn();
}

function Harness() {
  const [viewMode, setViewMode] = useState<TranscriptHistoryViewMode>("list");
  return (
    <LocaleProvider>
      <TranscriptionHistoryToolbar
        title="History"
        description="Recent transcripts"
        total={2}
        itemLabel="transcripts"
        searchValue=""
        onSearchChange={vi.fn()}
        searchPlaceholder="Search"
        searchAriaLabel="Search transcripts"
        clearSearchLabel="Clear search"
        viewMode={viewMode}
        onViewModeChange={setViewMode}
      />
    </LocaleProvider>
  );
}

describe("TranscriptionHistoryToolbar", () => {
  beforeEach(() => {
    window.localStorage.setItem(LANGUAGE_STORAGE_KEY, "en");
    vi.stubGlobal("ResizeObserver", ResizeObserverMock);
    vi.spyOn(HTMLElement.prototype, "offsetLeft", "get").mockImplementation(function (this: HTMLElement) {
      return this.getAttribute("aria-label") === "Grid view" ? 48 : 4;
    });
    vi.spyOn(HTMLElement.prototype, "offsetWidth", "get").mockImplementation(function (this: HTMLElement) {
      return this.classList.contains("t-tab") ? 40 : 0;
    });
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it("positions the pill without an entrance tween and slides it with the controlled view mode", () => {
    const { container } = render(<Harness />);
    const pill = container.querySelector(".t-tabs-pill") as HTMLSpanElement;

    expect(pill).toHaveAttribute("aria-hidden", "true");
    expect(pill.style.transform).toBe("translateX(4px)");
    expect(pill.style.width).toBe("40px");

    fireEvent.click(screen.getByRole("radio", { name: "Grid view" }));
    expect(screen.getByRole("radio", { name: "Grid view" })).toHaveAttribute("data-state", "on");
    expect(pill.style.transform).toBe("translateX(48px)");
    expect(pill.style.width).toBe("40px");
  });
});
