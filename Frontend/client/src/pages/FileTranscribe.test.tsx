import type { ReactNode } from "react";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { LocaleProvider, LANGUAGE_STORAGE_KEY } from "@/i18n";
import type { TranscriptHistoryItem } from "@/lib/api-types";
import FileTranscribe from "./FileTranscribe";

const { history, request, navigate, toast, view } = vi.hoisted(() => ({
  history: [] as TranscriptHistoryItem[],
  request: vi.fn(),
  navigate: vi.fn(),
  toast: vi.fn(),
  view: { mode: "grid" as "grid" | "list" },
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
  useTranscriptHistoryQuery: () => ({ items: history, total: history.length }),
}));
vi.mock("@/components/virtual-transcript-history", () => ({
  VirtualTranscriptHistory: ({
    items,
    renderItem,
  }: {
    items: TranscriptHistoryItem[];
    renderItem: (item: TranscriptHistoryItem) => ReactNode;
  }) => (
    <>
      {items.map((item) => (
        <div key={item.id}>{renderItem(item)}</div>
      ))}
    </>
  ),
}));

function mount(item: Partial<TranscriptHistoryItem>) {
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
  client.setQueryData(["/api/settings"], {});
  return render(
    <QueryClientProvider client={client}>
      <LocaleProvider>
        <FileTranscribe />
      </LocaleProvider>
    </QueryClientProvider>,
  );
}

describe("File transcription recovery", () => {
  beforeEach(() => {
    window.localStorage.setItem(LANGUAGE_STORAGE_KEY, "en");
    history.length = 0;
    view.mode = "grid";
    request.mockReset();
    request.mockResolvedValue({ ok: true, json: async () => ({ episode: null, resumeAvailable: false }) });
  });

  it.each(["grid", "list"] as const)(
    "offers retry for a failed upload in %s view without a podcast episode",
    async (mode) => {
      view.mode = mode;
      mount({});
      await waitFor(() => expect(screen.getByRole("button", { name: "Retry transcription" })).toBeEnabled());
    },
  );

  it("keeps transcription retry available when a failed record retains a pending summary marker", async () => {
    mount({ summaryStatus: "pending" });
    await waitFor(() => expect(screen.getByRole("button", { name: "Retry transcription" })).toBeEnabled());
  });

  it.each(["grid", "list"] as const)("resumes saved paid progress from %s without another upload", async (mode) => {
    view.mode = mode;
    request.mockResolvedValue({ ok: true, json: async () => ({ resumeAvailable: true }) });
    mount({});
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
