import type { ReactNode } from "react";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { LocaleProvider, LANGUAGE_STORAGE_KEY, formatFullDateTimeLabel, formatRelativeDateLabel } from "@/i18n";
import type { TranscriptHistoryItem, YouTubeSearchItem } from "@/lib/api-types";
import Youtube from "./Youtube";

const { history, request, view } = vi.hoisted(() => ({
  history: [] as TranscriptHistoryItem[],
  request: vi.fn(),
  view: { mode: "list" as "grid" | "list" },
}));

vi.mock("@/hooks/use-toast", () => ({ useToast: () => ({ toast: vi.fn() }) }));
vi.mock("@/lib/backend", () => ({ apiUrl: (path: string) => path }));
vi.mock("@/lib/fetch-with-timeout", () => ({ fetchWithTimeout: request }));
vi.mock("wouter", async (importOriginal) => ({
  ...(await importOriginal<typeof import("wouter")>()),
  useLocation: () => ["/youtube", vi.fn()],
}));
vi.mock("@/hooks/use-browser-youtube-import", () => ({ useBrowserYoutubeImport: () => undefined }));
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
  prependTranscriptHistoryItem: vi.fn(),
  transcriptHistoryQueryKey: () => ["/api/transcripts", { type: "youtube" }],
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

function video(overrides: Partial<TranscriptHistoryItem>): TranscriptHistoryItem {
  return {
    id: "video",
    title: "A video",
    type: "youtube",
    status: "completed",
    date: "2026-10-02",
    duration: "12:00",
    ...overrides,
  };
}

function mount() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <LocaleProvider>
        <Youtube />
      </LocaleProvider>
    </QueryClientProvider>,
  );
}

describe("YouTube page", () => {
  beforeEach(() => {
    window.localStorage.setItem(LANGUAGE_STORAGE_KEY, "en");
    window.history.replaceState(null, "", "/youtube");
    history.length = 0;
    view.mode = "list";
    request.mockReset();
  });

  it.each(["list", "grid"] as const)("hides placeholder channel names in the %s view", (mode) => {
    view.mode = mode;
    const createdAt = new Date().toISOString();
    history.push(
      video({ id: "known", title: "Known channel video", channel: "Scriber Labs", createdAt }),
      video({ id: "unknown", title: "Unknown channel video", channel: "Unknown", createdAt }),
      video({ id: "german", title: "German placeholder video", channelTitle: "Unbekannt", createdAt }),
      video({ id: "empty", title: "Empty channel video", channel: "  ", createdAt }),
    );
    mount();
    expect(screen.getByText(/Scriber Labs/)).toBeInTheDocument();
    expect(screen.queryByText(/Unknown$|Unbekannt|Unknown channel •/)).toBeNull();
    const dates = screen.getAllByText(formatRelativeDateLabel("en", createdAt));
    expect(dates).toHaveLength(4);
    expect(dates[0].tagName).toBe("TIME");
    expect(dates[0]).toHaveAttribute("title", formatFullDateTimeLabel("en", createdAt));
  });

  it("enables Find video only for a query and labels results without placeholder channels", async () => {
    const publishedAt = new Date().toISOString();
    const result: YouTubeSearchItem = {
      videoId: "abc",
      url: "https://www.youtube.com/watch?v=abc",
      title: "Search hit",
      description: "",
      channelTitle: "Unknown",
      publishedAt,
      thumbnailUrl: "",
      duration: "3:00",
      durationSeconds: 180,
    };
    request.mockResolvedValue({ ok: true, json: async () => ({ query: "hit", items: [result] }) });
    mount();
    const find = screen.getByRole("button", { name: "Find video" });
    expect(find).toBeDisabled();
    fireEvent.change(screen.getByLabelText("YouTube URL or search terms"), { target: { value: "  " } });
    expect(find).toBeDisabled();
    fireEvent.change(screen.getByLabelText("YouTube URL or search terms"), { target: { value: "hit" } });
    expect(find).toBeEnabled();
    expect(find).toHaveClass("ui-cta--sheen");
    fireEvent.click(find);
    await waitFor(() => expect(screen.getByText("Search hit")).toBeInTheDocument());
    const published = screen.getByText(formatRelativeDateLabel("en", publishedAt, { time: false }));
    expect(published.tagName).toBe("TIME");
    expect(published.parentElement).toHaveTextContent(/^Today$/);
  });
});
