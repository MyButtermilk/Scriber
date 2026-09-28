import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { Route, Router } from "wouter";
import { memoryLocation } from "wouter/memory-location";
import { LANGUAGE_STORAGE_KEY, LocaleProvider } from "@/i18n";
import type { TranscriptDetailResponse } from "@/lib/api-types";
import TranscriptDetail from "./TranscriptDetail";
import { AppScrollContainerContext } from "@/contexts/AppScrollContainerContext";

vi.mock("@/hooks/use-transcript-auto-refresh", () => ({ useTranscriptAutoRefresh: () => ({ isWsConnected: true }) }));
vi.mock("@/hooks/use-mobile", () => ({ useIsMobile: () => false }));

const rateLimitMessage =
  "Microsoft MAI Transcribe via OpenRouter is temporarily rate limited (HTTP 429). Wait briefly or switch transcription provider.";

function mount(content = "", step = rateLimitMessage, summary = "", type: TranscriptDetailResponse["type"] = "mic") {
  const record: TranscriptDetailResponse = {
    id: "failed-mic",
    title: "Live Mic",
    date: "Today",
    duration: "00:03",
    type,
    status: "failed",
    content,
    step,
    summary,
  };
  const client = new QueryClient({ defaultOptions: { queries: { retry: false, queryFn: async () => record } } });
  const location = memoryLocation({ path: "/transcript/failed-mic" });
  return render(
    <QueryClientProvider client={client}>
      <LocaleProvider>
        <Router hook={location.hook}>
          <AppScrollContainerContext.Provider value={{ current: null }}>
            <Route path="/transcript/:id">
              <TranscriptDetail />
            </Route>
          </AppScrollContainerContext.Provider>
        </Router>
      </LocaleProvider>
    </QueryClientProvider>,
  );
}

describe("failed transcript", () => {
  beforeEach(() => {
    window.localStorage.setItem(LANGUAGE_STORAGE_KEY, "en");
  });

  it("shows the persisted failure separately from transcript text and actions", async () => {
    mount();
    expect(await screen.findByRole("alert")).toHaveTextContent(rateLimitMessage);
    expect(screen.getByText("No transcript text captured.")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Copy transcript" })).toBeNull();
    expect(screen.getByRole("button", { name: "Export" })).toBeDisabled();
    expect(screen.queryByText(/\d+ words/)).toBeNull();
  });

  it("copies and counts only retained speech when a recording fails", async () => {
    const user = userEvent.setup();
    mount("Retained speech.");
    expect(await screen.findByRole("alert")).toHaveTextContent(rateLimitMessage);
    expect(screen.getByText("2 words")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Copy transcript" }));
    expect(await navigator.clipboard.readText()).toBe("Retained speech.");
    expect(screen.getByRole("button", { name: "Export" })).toBeEnabled();
  });

  it.each([
    ["Error", "mic"],
    ["Timeout", "mic"],
    ["Download error", "youtube"],
    ["Storage error", "file"],
    ["Storage error", "youtube"],
  ] as const)("excludes legacy %s content from a %s transcript", async (prefix, type) => {
    mount(`[${prefix}] ${rateLimitMessage}`, "Transcribing...", "Earlier summary", type);
    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent(rateLimitMessage);
    expect(alert).not.toHaveTextContent(`[${prefix}]`);
    expect(screen.queryByText(/\d+ words/)).toBeNull();
    expect(screen.queryByRole("button", { name: "Copy transcript" })).toBeNull();
    expect(screen.getByText("No transcript text captured.")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Export" })).toBeDisabled();
  });
});
