import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { TranscriptHistoryPanel } from "@/components/transcript-history-panel";
import { LocaleProvider } from "@/i18n";

const baseProps = {
  emptyState: <div>Nothing recorded</div>,
  errorDescription: "History unavailable",
  errorTitle: "Could not load history",
  isEmpty: false,
  isError: false,
  noMatchesState: <div>No matches</div>,
  onRetry: vi.fn(),
  searchTerm: "",
  viewMode: "list" as const,
};

describe("TranscriptHistoryPanel", () => {
  it("pulses a stable skeleton and reveals loaded content through the shared hooks", () => {
    const { container, rerender } = render(
      <LocaleProvider>
        <TranscriptHistoryPanel {...baseProps} isLoading>
          <div>Transcript row</div>
        </TranscriptHistoryPanel>
      </LocaleProvider>,
    );

    expect(screen.getByRole("status", { name: "Loading transcripts" })).toHaveClass("is-pulsing");
    expect(container.querySelector(".transcript-history-skeleton-item")).not.toBeNull();
    expect(screen.queryByText("Transcript row")).not.toBeInTheDocument();

    rerender(
      <LocaleProvider>
        <TranscriptHistoryPanel {...baseProps} isLoading={false}>
          <div>Transcript row</div>
        </TranscriptHistoryPanel>
      </LocaleProvider>,
    );

    expect(screen.getByText("Transcript row")).toBeInTheDocument();
    expect(container.querySelector(".transcript-history-reveal")).toHaveClass("is-revealed");
    expect(container.querySelector(".t-skel-skeleton")).toHaveAttribute("aria-hidden", "true");
    expect(container.querySelector(".t-skel-content")).toHaveAttribute("aria-hidden", "false");
  });

  it("preserves error retry and search-aware empty branches", () => {
    const onRetry = vi.fn();
    const { rerender } = render(
      <LocaleProvider>
        <TranscriptHistoryPanel {...baseProps} isError isLoading={false} onRetry={onRetry}>
          <div>Transcript row</div>
        </TranscriptHistoryPanel>
      </LocaleProvider>,
    );
    fireEvent.click(screen.getByRole("button"));
    expect(onRetry).toHaveBeenCalledTimes(1);

    rerender(
      <LocaleProvider>
        <TranscriptHistoryPanel {...baseProps} isEmpty isLoading={false} searchTerm="needle">
          <div>Transcript row</div>
        </TranscriptHistoryPanel>
      </LocaleProvider>,
    );
    expect(screen.getByText("No matches")).toBeInTheDocument();
  });
});
