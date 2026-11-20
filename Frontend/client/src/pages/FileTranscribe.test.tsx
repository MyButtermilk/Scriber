import type { ReactNode } from "react";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { LocaleProvider, LANGUAGE_STORAGE_KEY, formatFullDateTimeLabel, formatRelativeDateLabel } from "@/i18n";
import type { SettingsResponse, TranscriptHistoryItem } from "@/lib/api-types";
import FileTranscribe from "./FileTranscribe";

const { history, request, navigate, toast, view, historyInputs } = vi.hoisted(() => ({
  history: [] as TranscriptHistoryItem[],
  request: vi.fn(),
  navigate: vi.fn(),
  toast: vi.fn(),
  view: {
    mode: "grid" as "grid" | "list",
    limit: Infinity,
    replacement: undefined as TranscriptHistoryItem[] | undefined,
  },
  historyInputs: [] as TranscriptHistoryItem[][],
}));

vi.mock("@/hooks/use-toast", () => ({ useToast: () => ({ toast }) }));
vi.mock("@/lib/backend", () => ({ apiUrl: (path: string) => path }));
vi.mock("@/lib/fetch-with-timeout", () => ({ fetchWithTimeout: request }));
vi.mock("wouter", async (importOriginal) => ({
  ...(await importOriginal<typeof import("wouter")>()),
  useLocation: () => ["/file", navigate],
}));
vi.mock("@/hooks/use-transcript-auto-refresh", () => ({ useTranscriptAutoRefresh: () => ({ isWsConnected: true }) }));
vi.mock("@/hooks/use-transcript-history-panel-state", () => ({
  useTranscriptHistoryPanelState: () => ({
    debouncedSearch: "",
    searchValue: "",
    setSearchValue: vi.fn(),
    setViewMode: vi.fn(),
    viewMode: view.mode,
  }),
}));
vi.mock("@/hooks/use-transcript-history-query", () => ({
  transcriptHistoryQueryKey: () => ["/api/transcripts", { type: "file" }],
  useTranscriptHistoryQuery: () => ({
    items: view.replacement ?? history,
    total: (view.replacement ?? history).length,
  }),
}));
vi.mock("@/components/virtual-transcript-history", () => ({
  VirtualTranscriptHistory: ({
    items,
    renderItem,
  }: {
    items: TranscriptHistoryItem[];
    renderItem: (item: TranscriptHistoryItem) => ReactNode;
  }) => {
    historyInputs.push(items);
    return (
      <>
        {items.slice(0, view.limit).map((item) => (
          <div key={item.id}>{renderItem(item)}</div>
        ))}
      </>
    );
  },
}));

function mount(item: Partial<TranscriptHistoryItem>, settings: Partial<SettingsResponse> = {}) {
  history.push({
    id: "failed-file",
    title: "A failed uploaded recording.mp4",
    type: "file",
    status: "failed",
    date: "2026-10-02",
    duration: "2:17:33",
    summaryStatus: "idle",
    ...item,
  });
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  client.setQueryData(["/api/settings"], settings);
  const content = () => (
    <QueryClientProvider client={client}>
      <LocaleProvider>
        <FileTranscribe />
      </LocaleProvider>
    </QueryClientProvider>
  );
  const rendered = render(content());
  return { ...rendered, refresh: () => rendered.rerender(content()) };
}

describe("File transcription recovery", () => {
  beforeEach(() => {
    window.localStorage.setItem(LANGUAGE_STORAGE_KEY, "en");
    history.length = 0;
    view.mode = "grid";
    view.limit = Infinity;
    view.replacement = undefined;
    historyInputs.length = 0;
    request.mockReset();
    request.mockResolvedValue({ ok: true, json: async () => ({ episode: null, resumeAvailable: false }) });
  });

  it("copy feedback preserves the row model and avoids rescanning 10,000 loaded records", async () => {
    let statusReads = 0;
    view.limit = 1; // Model the visible window, rather than mounting the whole history in JSDOM.
    for (let index = 0; index < 10_000; index++) {
      history.push({
        id: `archive-${index}`,
        title: `Archived ${index}`,
        type: "file",
        date: "2026-10-02",
        duration: "1:00",
        get status() {
          statusReads++;
          return "completed" as const;
        },
      });
    }
    const writeText = vi.fn().mockResolvedValue(undefined);
    Object.defineProperty(navigator, "clipboard", { configurable: true, value: { writeText } });
    mount({ status: "completed" });
    const initialRows = historyInputs.at(-1);
    statusReads = 0;
    request.mockResolvedValue({ ok: true, json: async () => ({ content: "Copied speech" }) });
    fireEvent.click(screen.getByRole("button", { name: "Copy transcript Archived 0" }));
    await waitFor(() => expect(writeText).toHaveBeenCalledWith("Copied speech"));
    expect(statusReads).toBeLessThan(10);
    expect(historyInputs.at(-1) === initialRows).toBe(true);
  });

  it("repartitions history when processing records complete", () => {
    const mounted = mount({ status: "processing" });
    expect(historyInputs).toHaveLength(0);
    view.replacement = [{ ...history[0], status: "completed" }];
    mounted.refresh();
    expect(historyInputs.at(-1)).toEqual(view.replacement);
    expect(screen.getByRole("button", { name: "Copy transcript A failed uploaded recording.mp4" })).toBeEnabled();
  });

  it("keeps transcription retry available when a failed record retains a pending summary marker", async () => {
    mount({ summaryStatus: "pending" });
    await waitFor(() => expect(screen.getByRole("button", { name: "Retry transcription" })).toBeEnabled());
  });

  it.each(["grid", "list"] as const)("resumes saved paid progress from %s without another upload", async (mode) => {
    view.mode = mode;
    request.mockResolvedValue({ ok: true, json: async () => ({ resumeAvailable: true }) });
    mount({});
    expect(request).not.toHaveBeenCalled();
    const retry = screen.getByRole("button", { name: "Retry transcription" });
    expect(retry).toBeEnabled();
    fireEvent.click(retry);
    fireEvent.click(await screen.findByRole("button", { name: "Resume transcription" }));
    await waitFor(() =>
      expect(request).toHaveBeenCalledWith(
        "/api/transcripts/failed-file/resume-file",
        { method: "POST", credentials: "include" },
        60_000,
      ),
    );
    expect(screen.queryByRole("button", { name: "Retry transcription" })).toBeNull();
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(navigate).not.toHaveBeenCalled();
  });

  it.each(["grid", "list"] as const)("distinguishes audio and video files in the %s view", (mode) => {
    view.mode = mode;
    history.push(
      { id: "talk", title: "Keynote.MOV", type: "file", status: "completed", date: "2026-10-02", duration: "1:00" },
      { id: "call", title: "Call.m4a", type: "file", status: "completed", date: "2026-10-02", duration: "1:00" },
    );
    mount({ status: "completed", title: "Renamed recording" });
    expect(screen.getByRole("img", { name: "Video file" })).toHaveAttribute("data-media-kind", "video");
    expect(screen.getByRole("img", { name: "Audio file" })).toHaveAttribute("data-media-kind", "audio");
    // The ready tile carries the section accent, not a hard-coded primary tint.
    expect(screen.getByRole("img", { name: "Video file" }).closest(".file-history-icon")).toHaveClass(
      "file-history-icon--media",
    );
  });

  it("dates history cards relatively and keeps the full timestamp in the tooltip", () => {
    const createdAt = new Date().toISOString();
    mount({ status: "completed", createdAt });
    const date = screen.getByText(formatRelativeDateLabel("en", createdAt));
    expect(date.tagName).toBe("TIME");
    expect(date).toHaveAttribute("title", formatFullDateTimeLabel("en", createdAt));
  });

  it("summarizes upload limits inside the card with spaced units and provider details in a tooltip", async () => {
    mount(
      { status: "completed" },
      {
        fileUploadLimits: {
          provider: "mai",
          providerLabel: "Microsoft MAI Transcribe",
          usesDirectProviderLimit: true,
          audioMaxBytes: 2 * 1024 * 1024 * 1024,
          audioMaxLabel: "2GB",
          rawAudioIngestMaxBytes: 2 * 1024 * 1024 * 1024,
          rawAudioIngestMaxLabel: "2GB",
          videoMaxBytes: null,
          videoMaxLabel: null,
          compressionThresholdBytes: 50 * 1024 * 1024,
          compressionThresholdLabel: "50MB",
        },
      },
    );
    const upload = screen.getByRole("button", { name: "Upload file for transcription" });
    const summary = screen.getByText("Up to 2 GB · large files are optimized automatically");
    expect(upload).toContainElement(summary);
    expect(screen.queryByText(/Microsoft MAI Transcribe/)).toBeNull();
    const openPicker = vi.spyOn(HTMLInputElement.prototype, "click");
    const info = screen.getByRole("button", { name: "Upload limits" });
    fireEvent.click(info);
    const tooltip = await screen.findByRole("tooltip");
    // The info button sits inside the dropzone but must not open the file picker.
    expect(openPicker).not.toHaveBeenCalled();
    expect(tooltip).toHaveTextContent("Microsoft MAI Transcribe accepts audio up to 2 GB");
    expect(tooltip).toHaveTextContent("Files over 50 MB are optimized automatically before transcription.");
  });

  it("retries only the summary when transcription succeeded", async () => {
    mount({ status: "completed", summaryStatus: "failed" });
    fireEvent.click(screen.getByRole("button", { name: "Retry summary for A failed uploaded recording.mp4" }));
    await waitFor(() =>
      expect(request).toHaveBeenCalledWith(
        "/api/transcripts/failed-file/summarize",
        { method: "POST", credentials: "include" },
        15 * 60_000,
      ),
    );
    expect(navigate).not.toHaveBeenCalled();
  });
});
