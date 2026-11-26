import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { Route, Router } from "wouter";
import { memoryLocation } from "wouter/memory-location";
import { LANGUAGE_STORAGE_KEY, LocaleProvider } from "@/i18n";
import type { TranscriptDetailResponse } from "@/lib/api-types";
import TranscriptDetail from "./TranscriptDetail";
import { AppScrollContainerContext } from "@/contexts/AppScrollContainerContext";
import ReactMarkdown from "react-markdown";
import { fetchWithTimeout } from "@/lib/fetch-with-timeout";

vi.mock("react-markdown", async (importOriginal) => {
  const actual = await importOriginal<typeof import("react-markdown")>();
  return { ...actual, default: vi.fn(actual.default) };
});

vi.mock("@/hooks/use-transcript-auto-refresh", () => ({ useTranscriptAutoRefresh: () => ({ isWsConnected: true }) }));
vi.mock("@/hooks/use-mobile", () => ({ useIsMobile: () => false }));
const { markPodcastTranscriptRead } = vi.hoisted(() => ({ markPodcastTranscriptRead: vi.fn(async () => {}) }));
vi.mock("@/lib/podcast-progress", () => ({ markPodcastTranscriptRead }));
const { saveTranscriptExport, saveTranscriptPdfExport } = vi.hoisted(() => ({
  saveTranscriptExport: vi.fn(async () => ({ status: "cancelled" as const })),
  saveTranscriptPdfExport: vi.fn(async (_request: unknown) => ({ status: "cancelled" as const })),
}));
vi.mock("@/lib/transcript-export", () => ({ saveTranscriptExport, saveTranscriptPdfExport }));
const { defaultFetch } = vi.hoisted(() => ({
  defaultFetch: async (_input: RequestInfo | URL, _init?: RequestInit, _timeoutMs?: number) =>
    new Response(JSON.stringify({ episode: null })),
}));
vi.mock("@/lib/fetch-with-timeout", () => ({ fetchWithTimeout: vi.fn(defaultFetch) }));
// restoreMocks does not reset vi.fn implementations; a test override must not leak.
beforeEach(() => {
  vi.mocked(fetchWithTimeout).mockImplementation(defaultFetch);
});

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

function completed(id: string, content: string, overrides: Partial<TranscriptDetailResponse> = {}) {
  return {
    id,
    title: `Transcript ${id}`,
    date: "Today",
    duration: "01:00",
    type: "file",
    status: "completed",
    content,
    summary: "",
    summaryStatus: "completed",
    ...overrides,
  } satisfies TranscriptDetailResponse;
}

/** Mount the keyed transcript route exactly as App.tsx declares it. */
function mountRoute(records: Record<string, TranscriptDetailResponse>, path: string) {
  const client = new QueryClient({
    defaultOptions: {
      queries: {
        retry: false,
        queryFn: async ({ queryKey }) => records[String(queryKey[1])] ?? null,
      },
    },
  });
  const location = memoryLocation({ path, record: true });
  const view = render(
    <QueryClientProvider client={client}>
      <LocaleProvider>
        <Router hook={location.hook}>
          <AppScrollContainerContext.Provider value={{ current: null }}>
            <Route path="/transcript/:id">{(params) => <TranscriptDetail key={params.id} />}</Route>
          </AppScrollContainerContext.Provider>
        </Router>
      </LocaleProvider>
    </QueryClientProvider>,
  );
  return { ...view, client, location };
}

const canonicalContent = [
  "[0:00] Speaker 0: Über die Brücke gehen wir.",
  "[0:05] Speaker 1: Danach geht es über den Fluss.",
  "[125:04] Speaker 0: Und am Ende uber alles.",
].join("\n");

function sectionTrigger(name: RegExp) {
  return screen.getAllByRole("button", { name }).find((button) => button.hasAttribute("aria-expanded"))!;
}

function pressFind() {
  return fireEvent.keyDown(window, { key: "f", code: "KeyF", ctrlKey: true });
}

describe("completed transcript", () => {
  beforeEach(() => {
    window.localStorage.setItem(LANGUAGE_STORAGE_KEY, "en");
    HTMLElement.prototype.scrollIntoView = vi.fn();
  });

  it("renders canonical segments as timestamp and speaker rows, including Speaker 0", async () => {
    const { container } = mountRoute({ a: completed("a", canonicalContent) }, "/transcript/a");
    expect(await screen.findByText("Über die Brücke gehen wir.")).toBeInTheDocument();
    const headers = Array.from(container.querySelectorAll("[data-transcript-block-header]"));
    expect(headers.map((header) => header.textContent)).toEqual(["0:00Speaker 0", "0:05Speaker 1", "2:05:04Speaker 0"]);
    expect(screen.queryByText(/\[0:00\]/)).toBeNull();
    expect(container.querySelectorAll("[data-transcript-speaker]")[0].className).toContain("bg-blue-100");
  });

  it("keeps plain transcript line breaks", async () => {
    const { container } = mountRoute({ a: completed("a", "Line one\nLine two\n\nNext paragraph") }, "/transcript/a");
    await screen.findByText("Next paragraph");
    const paragraphs = Array.from(container.querySelectorAll(".transcript-content p"));
    expect(paragraphs.map((paragraph) => paragraph.textContent)).toEqual(["Line one\nLine two", "Next paragraph"]);
    expect(paragraphs[0].className).toContain("whitespace-pre-wrap");
  });

  it("opens an existing summary first and adds a summary that arrives without collapsing the transcript", async () => {
    const records = {
      a: completed("a", "Reading this transcript.", { summaryStatus: "pending" }),
      b: completed("b", "Other transcript.", { summary: "## Saved summary" }),
    };
    const first = mountRoute(records, "/transcript/a");
    await screen.findByText("Reading this transcript.");
    expect(sectionTrigger(/^Transcript/)).toHaveAttribute("aria-expanded", "true");
    expect(sectionTrigger(/^Summary/)).toHaveAttribute("aria-expanded", "false");

    records.a = { ...records.a, summary: "## Arrived summary", summaryStatus: "completed" };
    first.client.setQueryData(["/api/transcripts", "a"], records.a);
    expect(await screen.findByRole("heading", { name: "Arrived summary" })).toBeInTheDocument();
    await waitFor(() => expect(sectionTrigger(/^Summary/)).toHaveAttribute("aria-expanded", "true"));
    expect(sectionTrigger(/^Transcript/)).toHaveAttribute("aria-expanded", "true");
    first.unmount();

    mountRoute(records, "/transcript/b");
    expect(await screen.findByRole("heading", { name: "Saved summary" })).toBeInTheDocument();
    await waitFor(() => expect(sectionTrigger(/^Summary/)).toHaveAttribute("aria-expanded", "true"));
    expect(sectionTrigger(/^Transcript/)).toHaveAttribute("aria-expanded", "false");
  });

  it("resets page state when navigating to another transcript", async () => {
    const user = userEvent.setup();
    const { location } = mountRoute(
      { a: completed("a", "Alpha words here."), b: completed("b", "Beta words here.") },
      "/transcript/a",
    );
    await screen.findByText("Alpha words here.");
    await user.click(sectionTrigger(/^Transcript/));
    expect(sectionTrigger(/^Transcript/)).toHaveAttribute("aria-expanded", "false");
    await user.click(screen.getByRole("button", { name: "Find in transcript" }));
    expect(screen.getByRole("searchbox", { name: "Search transcript" })).toBeInTheDocument();

    act(() => location.navigate("/transcript/b"));
    expect(await screen.findByText("Beta words here.")).toBeInTheDocument();
    expect(screen.queryByRole("searchbox", { name: "Search transcript" })).toBeNull();
    expect(sectionTrigger(/^Transcript/)).toHaveAttribute("aria-expanded", "true");
  });

  it("returns from a podcast transcript to its podcast", async () => {
    vi.mocked(fetchWithTimeout).mockImplementation(async (input) =>
      String(input).includes("/api/podcasts/transcripts/")
        ? Response.json({ episode: { id: "episode-1", status: "completed", subscriptionId: "sub 1/2" } })
        : Response.json({}),
    );
    mountRoute({ p: completed("p", "Podcast words.") }, "/transcript/p");
    await screen.findByText("Podcast words.");
    await waitFor(() =>
      expect(screen.getByRole("button", { name: "Go back" }).closest("a")).toHaveAttribute(
        "href",
        `/podcasts?subscription=${encodeURIComponent("sub 1/2")}`,
      ),
    );
    expect(
      vi.mocked(fetchWithTimeout).mock.calls.some(([url]) => String(url).includes("/api/podcasts/transcripts/p")),
    ).toBe(true);
  });

  it("returns from an ordinary uploaded file to File history", async () => {
    mountRoute({ f: completed("f", "Uploaded words.") }, "/transcript/f");
    await screen.findByText("Uploaded words.");
    await waitFor(() =>
      expect(vi.mocked(fetchWithTimeout).mock.calls.some(([url]) => String(url).includes("/api/podcasts/"))).toBe(true),
    );
    expect(screen.getByRole("button", { name: "Go back" }).closest("a")).toHaveAttribute("href", "/file");
  });

  it("Ctrl+F expands the transcript, focuses search, cycles matches, and Escape clears them", async () => {
    const user = userEvent.setup();
    const { container } = mountRoute(
      { a: completed("a", canonicalContent, { summary: "## Summary first" }) },
      "/transcript/a",
    );
    await screen.findByRole("heading", { name: "Summary first" });
    await waitFor(() => expect(sectionTrigger(/^Transcript/)).toHaveAttribute("aria-expanded", "false"));

    expect(pressFind()).toBe(false);
    const input = await screen.findByRole("searchbox", { name: "Search transcript" });
    await waitFor(() => expect(input).toHaveFocus());
    expect(sectionTrigger(/^Transcript/)).toHaveAttribute("aria-expanded", "true");
    expect(sectionTrigger(/^Summary/)).toHaveAttribute("aria-expanded", "true");

    await user.type(input, "UBER");
    const counter = container.querySelector("[data-transcript-find-counter]")!;
    expect(counter).toHaveAttribute("aria-live", "polite");
    await waitFor(() => expect(counter).toHaveTextContent("1 / 3"));
    const marks = () => Array.from(container.querySelectorAll("mark"));
    expect(marks().map((mark) => mark.textContent)).toEqual(["Über", "über", "uber"]);
    expect(marks()[0]).toHaveAttribute("data-active", "true");
    expect(HTMLElement.prototype.scrollIntoView).toHaveBeenCalled();

    await user.keyboard("{Enter}");
    expect(counter).toHaveTextContent("2 / 3");
    expect(marks()[1]).toHaveAttribute("data-active", "true");
    await user.keyboard("{Enter}{Enter}");
    expect(counter).toHaveTextContent("1 / 3");
    await user.keyboard("{Shift>}{Enter}{/Shift}");
    expect(counter).toHaveTextContent("3 / 3");
    expect(marks()[2]).toHaveAttribute("data-active", "true");

    await user.clear(input);
    await user.type(input, "zzz");
    await waitFor(() => expect(counter).toHaveTextContent("0 / 0"));
    expect(marks()).toHaveLength(0);

    await user.type(input, "{Escape}");
    expect(screen.queryByRole("searchbox", { name: "Search transcript" })).toBeNull();
    expect(container.querySelectorAll("mark")).toHaveLength(0);
    expect(screen.getByRole("button", { name: "Find in transcript" })).toHaveFocus();

    // Reopening starts with an empty query.
    pressFind();
    expect(await screen.findByRole("searchbox", { name: "Search transcript" })).toHaveValue("");
  });

  it("leaves Ctrl+F to the browser when there is no transcript text", async () => {
    mountRoute({ e: completed("e", "") }, "/transcript/e");
    await screen.findByText("No transcript text captured.");
    expect(pressFind()).toBe(true);
    expect(screen.queryByRole("searchbox")).toBeNull();
  });

  it("groups multi-speaker turns with rails and lets users focus one speaker without hiding search matches", async () => {
    const user = userEvent.setup();
    const { container } = mountRoute({ a: completed("a", canonicalContent) }, "/transcript/a");
    await screen.findByText("Über die Brücke gehen wir.");

    const turns = Array.from(container.querySelectorAll("[data-transcript-speaker-turn]"));
    expect(turns.map((turn) => turn.getAttribute("data-transcript-speaker-turn"))).toEqual([
      "Speaker 0",
      "Speaker 1",
      "Speaker 0",
    ]);

    const filterGroup = container.querySelector("[data-transcript-speaker-filters]");
    expect(filterGroup).not.toBeNull();
    const speaker1Chip = container.querySelector<HTMLButtonElement>('[data-transcript-speaker-filter="Speaker 1"]')!;
    await user.click(speaker1Chip);
    expect(speaker1Chip).toHaveAttribute("aria-pressed", "true");
    expect(turns[0]).toHaveAttribute("data-dimmed", "true");
    expect(turns[1]).not.toHaveAttribute("data-dimmed");
    expect(turns[2]).toHaveAttribute("data-dimmed", "true");

    // Searching keeps matching turns readable even when filtered to another speaker.
    pressFind();
    const input = await screen.findByRole("searchbox", { name: "Search transcript" });
    await user.type(input, "Brücke");
    await waitFor(() => expect(turns[0]).not.toHaveAttribute("data-dimmed"));
    expect(turns[2]).toHaveAttribute("data-dimmed", "true");

    const allChip = container.querySelector<HTMLButtonElement>('[data-transcript-speaker-filter="all"]')!;
    await user.click(allChip);
    expect(allChip).toHaveAttribute("aria-pressed", "true");
    expect(turns.every((turn) => !turn.hasAttribute("data-dimmed"))).toBe(true);
  });

  it("renders single-speaker timed transcripts in an editorial layout with logical section breaks", async () => {
    const monologue = [
      "[0:09] All right. It's true. The last video you watched on this channel is the last video you will ever see from that space, which is bittersweet, for sure... But this video is the first one ever shot in this space, and that's very exciting. This, frankly, is... pretty insane. This is the biggest project I've ever done. It is this whole building. It is two stories. It's about 30,000 square feet, and it's going to take about the next year or so from the time of... shooting this... to build it all up. Now, here's the thing. I'm not an architect, I'm not a interior designer, I'm not a professional floor planner, any of that stuff... All I know is exactly what I want the dream video studio to",
      "[0:57] look and feel and work like. And so I found the space and... now we're going to do it. Now, it's funny, most of the advice that I've gotten has been basically not to do this... And that's understandable. Like, it's already inherently risky to empty the piggy bank into a construction project. So I get it. But also, like, anytime it's a big investment like you see this in cars, too, people always talk about, like, building it for the person you're going to sell it to later when you're done. Like, think about making it sellable. Think about the next guy. But I've kind of just... thrown all of that away... just for the love of the game.",
      "[1:37] Because I know that the video production space that I want this to be, this doesn't exist in any other form. Like, this is going to be a brand new thing. So, yeah, not really thinking all about who else is going to buy it after me. This is just... going to be the best possible space for what it's designed for. It actually took more than a year to find this space, and I guess there's about a year of work probably left ahead of us for construction... Now what's fun for you... is this is being recorded... in secret a year ago from when you're watching it. And the next video you get to watch is the finished studio, which is sick. So this is going to be an empty building tour,",
      "[2:12] and we will walk through every single room together from start to finish.",
    ].join("\n");

    const { container } = mountRoute({ m: completed("m", monologue) }, "/transcript/m");
    await screen.findByText(/dream video studio to look and feel and work like\./);
    expect(container.querySelector("[data-transcript-speaker-filters]")).toBeNull();
    expect(container.querySelectorAll("[data-transcript-block]").length).toBeGreaterThanOrEqual(4);
    expect(container.querySelectorAll('[data-transcript-section-break="true"]').length).toBeGreaterThanOrEqual(1);
  });
});

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

  it("counts opening a completed file transcript as reading its podcast episode", async () => {
    const { client } = mount("Transcript text.", "", "", "file", { id: "c".repeat(32), status: "completed" });
    await screen.findByText("Transcript text.");
    await waitFor(() => expect(markPodcastTranscriptRead).toHaveBeenCalledWith(client, "c".repeat(32)));
  });

  it("marks a podcast transcript on each visit without repeating the mark on a data refresh", async () => {
    const id = "c".repeat(32);
    const firstVisit = mount("Transcript text.", "", "Saved summary.", "file", {
      id,
      status: "completed",
      summaryStatus: "completed",
    });
    await screen.findByText("Transcript text.");
    await waitFor(() => expect(markPodcastTranscriptRead).toHaveBeenCalledTimes(1));

    firstVisit.client.setQueryData<TranscriptDetailResponse>(["/api/transcripts", "failed-mic"], (current) =>
      current ? { ...current, content: "Refreshed transcript text." } : current,
    );
    await screen.findByText("Refreshed transcript text.");
    expect(markPodcastTranscriptRead).toHaveBeenCalledTimes(1);
    firstVisit.unmount();

    const secondVisit = mount("Transcript text.", "", "Saved summary.", "file", {
      id,
      status: "completed",
      summaryStatus: "completed",
    });
    await waitFor(() => expect(markPodcastTranscriptRead).toHaveBeenCalledTimes(2));
    expect(markPodcastTranscriptRead).toHaveBeenLastCalledWith(secondVisit.client, id);
  });

  it("does not mark failed transcripts as read", async () => {
    mount("", "Podcast processing failed.", "", "file", { id: "c".repeat(32) });
    await screen.findByText("Podcast processing failed.");
    expect(markPodcastTranscriptRead).not.toHaveBeenCalled();
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

  it("exports the summary of a YouTube transcript as its own PDF with localized labels", async () => {
    const user = userEvent.setup();
    mount("Transcript text.", "", "## Thema\n\nAbsatz.", "youtube", {
      status: "completed",
      summaryStatus: "completed",
    });
    await user.click(await screen.findByRole("button", { name: "Export" }));
    await user.click(await screen.findByRole("menuitem", { name: "Export summary as PDF" }));

    await waitFor(() => expect(saveTranscriptPdfExport).toHaveBeenCalledTimes(1));
    const request = saveTranscriptPdfExport.mock.calls[0][0] as {
      transcriptId: string;
      variant: string;
      labels: { locale: string; summary: string; date: string; pageOf: string; speaker: (n: number) => string };
    };
    expect(request.transcriptId).toBe("failed-mic");
    expect(request.variant).toBe("summary");
    expect(request.labels.locale).toBe("en-US");
    expect(request.labels.summary).toBe("Summary");
    expect(request.labels.date).toBe("Date");
    expect(request.labels.pageOf).toBe("Page {page} of {total}");
    expect(request.labels.speaker(2)).toBe("Speaker 2");
    expect(saveTranscriptExport).not.toHaveBeenCalled();
  });

  it("routes the PDF export through the WebView typesetter and offers no summary PDF without a summary", async () => {
    const user = userEvent.setup();
    mount("Transcript text.", "", "", "file", { status: "completed" });
    await user.click(await screen.findByRole("button", { name: "Export" }));
    expect(screen.queryByRole("menuitem", { name: "Export summary as PDF" })).toBeNull();
    await user.click(await screen.findByRole("menuitem", { name: "Export as PDF" }));

    await waitFor(() => expect(saveTranscriptPdfExport).toHaveBeenCalledTimes(1));
    expect((saveTranscriptPdfExport.mock.calls[0][0] as { variant: string }).variant).toBe("full");
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
