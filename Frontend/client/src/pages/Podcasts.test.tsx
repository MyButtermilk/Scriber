import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { LocaleProvider, LANGUAGE_STORAGE_KEY } from "@/i18n";
import {
  podcastStatusLabels,
  type PodcastEpisode,
  type PodcastLibrary,
  type PodcastSubscription,
} from "@/lib/podcast-types";
import { germanTranslations } from "@/i18n/translations/de";
import Podcasts from "./Podcasts";

const { request, toast } = vi.hoisted(() => ({ request: vi.fn(), toast: vi.fn() }));
vi.mock("@/lib/queryClient", () => ({ apiRequest: request }));
vi.mock("@/hooks/use-toast", () => ({ useToast: () => ({ toast }) }));
vi.mock("@/lib/backend", () => ({ apiUrl: (path: string) => path }));

function subscription(): PodcastSubscription {
  return {
    id: "a".repeat(32),
    title: "Thoughtful conversations",
    author: "Studio",
    description: "A podcast about science and culture",
    feedUrl: "https://example.com/feed",
    autoProcess: true,
    lastCheckedAt: "2026-09-07T12:00:00Z",
    error: "",
    episodeCount: 3,
    completedCount: 1,
  };
}

function episode(overrides: Partial<PodcastEpisode> = {}): PodcastEpisode {
  return {
    id: "b".repeat(32),
    title: "Understanding tomorrow",
    description: "A long, interesting conversation.",
    publishedAt: "2026-09-07T12:00:00Z",
    durationSeconds: 1200,
    status: "available",
    transcriptId: "",
    downloadedBytes: 0,
    error: "",
    ...overrides,
  };
}

function mount(subscriptions: PodcastSubscription[] = []) {
  const library: PodcastLibrary = {
    subscriptions,
    activeCount: 0,
    refreshIntervalMinutes: 30,
    cacheLimitBytes: 2 * 1024 ** 3,
    running: true,
    refreshing: false,
  };
  const client = new QueryClient({ defaultOptions: { queries: { retry: false, queryFn: async () => library } } });
  return render(
    <QueryClientProvider client={client}>
      <LocaleProvider>
        <Podcasts />
      </LocaleProvider>
    </QueryClientProvider>,
  );
}

describe("Podcasts", () => {
  it("offers an audio-only download after a completed episode's local copy is removed", async () => {
    const sub = subscription();
    const completed = episode({ status: "completed", transcriptId: "existing-transcript", downloadedBytes: 0 });
    request.mockImplementation(async (_method: string, path: string) => ({
      json: async () => (path.includes("/episodes?") ? { items: [completed], total: 1 } : { ok: true }),
    }));
    mount([sub]);
    fireEvent.click(await screen.findByRole("button", { name: "Download audio again" }));
    await waitFor(() =>
      expect(request).toHaveBeenCalledWith("POST", `/api/podcasts/episodes/${completed.id}/queue`, undefined, {
        timeoutMs: 60_000,
      }),
    );
  });

  it("provides German text for every server-side episode state", () => {
    for (const label of Object.values(podcastStatusLabels)) {
      expect(germanTranslations[label]).toBeTruthy();
    }
  });

  beforeEach(() => {
    window.localStorage.setItem(LANGUAGE_STORAGE_KEY, "en");
    request.mockReset();
    request.mockImplementation(async () => ({ json: async () => ({ items: [], total: 0 }) }));
  });

  it("keeps search explicit and subscribes using the selected public RSS feed", async () => {
    request.mockImplementation(async (_method: string, path: string) => ({
      json: async () =>
        path.startsWith("/api/podcasts/search")
          ? { items: [{ id: "found", title: "Search result", author: "Creator", feedUrl: "https://example.com/show" }] }
          : { id: "new-subscription" },
    }));
    mount();
    await screen.findByText("Your next good listen starts here");
    fireEvent.change(screen.getByRole("textbox", { name: "Find a podcast" }), { target: { value: "A & B" } });
    expect(request).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "Search" }));
    await screen.findByText("Search result");
    expect(request).toHaveBeenCalledWith("GET", "/api/podcasts/search?q=A%20%26%20B");
    fireEvent.click(screen.getByRole("button", { name: "Subscribe" }));
    await waitFor(() =>
      expect(request).toHaveBeenCalledWith(
        "POST",
        "/api/podcasts/subscriptions",
        { feedUrl: "https://example.com/show", autoProcess: true },
        { timeoutMs: 60_000 },
      ),
    );
  });

  it("pauses new automatic work and offers an explicit retry for a failed episode", async () => {
    const sub = subscription();
    request.mockImplementation(async (_method: string, path: string) => ({
      json: async () =>
        path.includes("/episodes?")
          ? { items: [episode({ status: "failed", error: "Check provider settings" })], total: 1 }
          : { ok: true },
    }));
    mount([sub]);
    await screen.findByText("Understanding tomorrow");
    fireEvent.click(screen.getByRole("switch", { name: "Automatically process new episodes" }));
    await waitFor(() =>
      expect(request).toHaveBeenCalledWith(
        "PATCH",
        `/api/podcasts/subscriptions/${sub.id}`,
        { autoProcess: false },
        { timeoutMs: 60_000 },
      ),
    );
    await waitFor(() => expect(screen.getByRole("button", { name: "Retry episode" })).not.toBeDisabled());
    fireEvent.click(screen.getByRole("button", { name: "Retry episode" }));
    await waitFor(() =>
      expect(request).toHaveBeenCalledWith("POST", `/api/podcasts/episodes/${"b".repeat(32)}/queue`, undefined, {
        timeoutMs: 60_000,
      }),
    );
  });

  it("opens existing transcripts and plays only downloaded local audio", async () => {
    request.mockResolvedValue({
      json: async () => ({
        items: [episode({ status: "completed", transcriptId: "transcript-1", downloadedBytes: 500 })],
        total: 1,
      }),
    });
    const { container } = mount([subscription()]);
    expect(await screen.findByRole("link", { name: "Open transcript" })).toHaveAttribute(
      "href",
      "/transcript/transcript-1",
    );
    expect(container.querySelector("audio")).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Listen" }));
    expect(container.querySelector("audio")).toHaveAttribute("src", `/api/podcasts/episodes/${"b".repeat(32)}/audio`);
    fireEvent.click(screen.getByRole("button", { name: "Close player" }));
    expect(container.querySelector("audio")).toBeNull();
  });

  it("explains background scope and provider costs through an accessible info control", async () => {
    mount();
    await screen.findByText("Your next good listen starts here");
    fireEvent.focus(screen.getByRole("button", { name: "How podcast subscriptions work" }));
    const tip = screen.getByRole("tooltip");
    expect(within(tip).getByText(/while Scriber is running/)).toBeVisible();
    expect(within(tip).getByText(/normal usage charges may apply/)).toBeVisible();
    fireEvent.keyDown(document, { key: "Escape" });
    expect(screen.queryByRole("tooltip")).toBeNull();
  });
});
