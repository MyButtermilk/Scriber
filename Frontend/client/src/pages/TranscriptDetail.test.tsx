import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { Route, Router } from "wouter";
import { memoryLocation } from "wouter/memory-location";
import { LANGUAGE_STORAGE_KEY, LocaleProvider } from "@/i18n";
import type { TranscriptDetailResponse } from "@/lib/api-types";
import TranscriptDetail from "./TranscriptDetail";
import { AppScrollContainerContext } from "@/contexts/AppScrollContainerContext";
import ReactMarkdown from "react-markdown";

vi.mock("react-markdown", async (importOriginal) => {
  const actual = await importOriginal<typeof import("react-markdown")>();
  return { ...actual, default: vi.fn(actual.default) };
});

vi.mock("@/hooks/use-transcript-auto-refresh", () => ({ useTranscriptAutoRefresh: () => ({ isWsConnected: true }) }));
vi.mock("@/hooks/use-mobile", () => ({ useIsMobile: () => false }));
vi.mock("@/lib/fetch-with-timeout", () => ({
  fetchWithTimeout: vi.fn(async () => new Response(JSON.stringify({ episode: null }))),
}));

const rateLimitMessage =
  "Microsoft MAI Transcribe via OpenRouter is temporarily rate limited (HTTP 429). Wait briefly or switch transcription provider.";

function mount(
  content = "",
  step = rateLimitMessage,
  summary = "",
  type: TranscriptDetailResponse["type"] = "mic",
  overrides: Partial<TranscriptDetailResponse> = {},
) {
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
    ...overrides,
  };
  const client = new QueryClient({ defaultOptions: { queries: { retry: false, queryFn: async () => record } } });
  const location = memoryLocation({ path: "/transcript/failed-mic" });
  const view = render(
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
  return { ...view, client };
}

describe("failed transcript", () => {
  beforeEach(() => {
    window.localStorage.setItem(LANGUAGE_STORAGE_KEY, "en");
  });

  it("copying a long completed summary does not reparse unchanged Markdown", async () => {
    const user = userEvent.setup();
    const summary = Array.from({ length: 200 }, (_, i) => `## Topic ${i}\n\nParagraph **${i}**.`).join("\n\n");
    mount("Transcript text.", "", summary, "file", { status: "completed", summaryStatus: "completed" });
    await screen.findByRole("heading", { name: "Topic 199" });
    vi.mocked(ReactMarkdown).mockClear();
    await user.click(screen.getByRole("button", { name: "Copy summary" }));
    expect(await navigator.clipboard.readText()).toBe(summary);
    expect(screen.getByRole("button", { name: "Copied!" })).toBeInTheDocument();
    expect(vi.mocked(ReactMarkdown).mock.calls.length).toBe(0);
  });

  it("updates a memoized summary when its saved content changes", async () => {
    const { client } = mount("Transcript", "", "## Original summary", "file", {
      status: "completed",
      summaryStatus: "completed",
    });
    await screen.findByRole("heading", { name: "Original summary" });
    client.setQueryData<TranscriptDetailResponse>(["/api/transcripts", "failed-mic"], (current) =>
      current ? { ...current, summary: "## Revised summary" } : current,
    );
    expect(await screen.findByRole("heading", { name: "Revised summary" })).toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: "Original summary" })).not.toBeInTheDocument();
  });

  it("offers transcription retry beside the error for an ordinary uploaded file", async () => {
    mount("[Error] Cannot connect to host openrouter.ai:443", "Failed", "", "file");
    const button = await screen.findByRole("button", { name: "Retry transcription" });
    await waitFor(() => expect(button).toBeEnabled());
    expect(button.closest(".space-y-2")).toHaveTextContent("Transcription failed");
    fireEvent.click(button);
    expect(await screen.findByRole("dialog")).toBeVisible();
  });

  it("offers summary retry even when an earlier successful summary remains", async () => {
    mount("Transcript text.", "Failed", "An earlier summary.", "file", {
      status: "completed",
      summaryStatus: "failed",
    });
    expect(await screen.findByRole("button", { name: "Retry Summary" })).toBeEnabled();
  });

  it("does not show stale summary processing after the transcription failed", async () => {
    mount("[Error] Connection failed", "Summarizing...", "", "file", { summaryStatus: "pending" });
    await screen.findByRole("button", { name: "Retry transcription" });
    expect(screen.queryByText("Summarizing...")).not.toBeInTheDocument();
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

  it.each(["failed", "stopped"] as const)(
    "offers resume for a %s file only when the backend confirms eligibility",
    async (status) => {
      mount("Retained speech.", "", "", "file", { status, resumeAvailable: true });
      expect(await screen.findByRole("button", { name: "Resume transcription" })).toBeEnabled();
      expect(screen.queryByRole("button", { name: "Retry transcription" })).toBeNull();
      expect(screen.queryByRole("dialog")).toBeNull();
    },
  );

  it("offers a new upload retry when saved progress is explicitly unavailable", async () => {
    mount("Retained speech.", "Failed", "", "file", { resumeAvailable: false });
    const retry = await screen.findByRole("button", { name: "Retry transcription" });
    await waitFor(() => expect(retry).toBeEnabled());
    expect(screen.queryByRole("button", { name: "Resume transcription" })).toBeNull();
    fireEvent.click(retry);
    expect(await screen.findByRole("dialog")).toBeVisible();
  });

  it.each([
    { type: "file", status: "failed", resumeAvailable: false },
    { type: "file", status: "stopped" },
    { type: "file", status: "processing", resumeAvailable: true },
    { type: "mic", status: "failed", resumeAvailable: true },
  ] satisfies Partial<TranscriptDetailResponse>[])(
    "does not offer resume without an eligible terminal file: %j",
    async (overrides) => {
      mount("Retained speech.", "", "", "file", overrides);
      await screen.findByRole("heading", { name: "Live Mic" });
      expect(screen.queryByRole("button", { name: "Resume transcription" })).toBeNull();
    },
  );

  it("shows uncertain joins at their original recording times without inserting warnings into speech", async () => {
    mount("Yes. Yes.", "", "", "file", {
      status: "completed",
      chunkBoundaryWarnings: [
        {
          code: "overlap_missing_word_timestamps",
          leftPartIndex: 0,
          rightPartIndex: 1,
          startMs: 3_601_000,
          endMs: 3_609_000,
        },
      ],
    });
    expect(await screen.findByRole("status")).toHaveTextContent("1:00:01–1:00:09");
    expect(screen.getByText("Yes. Yes.")).toBeInTheDocument();
    expect(screen.getByText("2 words")).toBeInTheDocument();
  });
});
