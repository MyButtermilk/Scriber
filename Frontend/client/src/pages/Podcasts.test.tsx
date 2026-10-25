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

function subscription(overrides: Partial<PodcastSubscription> = {}): PodcastSubscription {
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
    newCount: 0,
    unreadCount: 0,
    attentionCount: 0,
    ...overrides,
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
    isNew: false,
    readAt: "",
    playedAt: "",
    isUnread: false,
    ...overrides,
  };
}

function mount(subscriptions: PodcastSubscription[] = []) {
  const library: PodcastLibrary = {
    subscriptions,
    newCount: subscriptions.reduce((total, item) => total + item.newCount, 0),
    unreadCount: subscriptions.reduce((total, item) => total + item.unreadCount, 0),
    attentionCount: subscriptions.reduce((total, item) => total + item.attentionCount, 0),
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
  it("shows the linked attempt reference beside an episode error", async () => {
    const failed = episode({ status: "failed", transcriptId: "c".repeat(32), error: "Podcast processing failed." });
    request.mockImplementation(async (_method: string, path: string) => ({
      json: async () => (path.includes("/episodes?") ? { items: [failed], total: 1 } : { ok: true }),
    }));
    mount([subscription()]);
    expect(await screen.findByText(`Reference: ${failed.transcriptId}`)).toBeVisible();
  });

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
    window.history.replaceState(null, "", "/podcasts");
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

  it("highlights new episodes for this visit and records that they were seen", async () => {
    const fresh = episode({ isNew: true });
    request.mockImplementation(async (_method: string, path: string) => ({
      json: async () => (path.includes("/episodes?") ? { items: [fresh], total: 1 } : { updated: 1 }),
    }));
    mount([subscription({ newCount: 1, attentionCount: 1 })]);
    const row = (await screen.findByText("Understanding tomorrow")).closest("article");
    expect(row).toHaveAttribute("data-new", "true");
    expect(within(row!).getByText("New")).toBeVisible();
    expect(screen.getByLabelText("1 new episode")).toHaveTextContent("1 new");
    await waitFor(() =>
      expect(request).toHaveBeenCalledWith("POST", "/api/podcasts/episodes/seen", { ids: [fresh.id] }),
    );
    expect(request.mock.calls.filter(([, path]) => path === "/api/podcasts/episodes/seen")).toHaveLength(1);
  });

  it("does not mark episodes as seen while Scriber is in the background", async () => {
    const visibility = vi.spyOn(document, "visibilityState", "get").mockReturnValue("hidden");
    try {
      request.mockImplementation(async (_method: string, path: string) => ({
        json: async () => (path.includes("/episodes?") ? { items: [episode({ isNew: true })], total: 1 } : {}),
      }));
      mount([subscription({ newCount: 1, attentionCount: 1 })]);
      await screen.findByText("Understanding tomorrow");
      expect(request.mock.calls.some(([, path]) => path === "/api/podcasts/episodes/seen")).toBe(false);
      visibility.mockReturnValue("visible");
      fireEvent(document, new Event("visibilitychange"));
      await waitFor(() =>
        expect(request).toHaveBeenCalledWith("POST", "/api/podcasts/episodes/seen", { ids: ["b".repeat(32)] }),
      );
    } finally {
      visibility.mockRestore();
    }
  });

  it("filters to unread episodes and marks one as read from its options", async () => {
    const unread = episode({ status: "completed", transcriptId: "d".repeat(32), isUnread: true });
    request.mockImplementation(async (_method: string, path: string) => ({
      json: async () => (path.includes("/episodes?") ? { items: [unread], total: 1 } : { ok: true }),
    }));
    mount([subscription({ unreadCount: 2, attentionCount: 2 })]);
    const row = (await screen.findByText("Understanding tomorrow")).closest("article");
    expect(row).toHaveAttribute("data-unread", "true");
    expect(within(row!).getByText("Unread")).toBeVisible();
    expect(screen.getByText("2 unread")).toBeVisible();

    fireEvent.click(screen.getByRole("button", { name: "Unread (2)" }));
    await waitFor(() =>
      expect(request).toHaveBeenCalledWith(
        "GET",
        `/api/podcasts/subscriptions/${"a".repeat(32)}/episodes?offset=0&view=unread`,
      ),
    );
    expect(screen.getByRole("button", { name: "Unread (2)" })).toHaveAttribute("aria-pressed", "true");

    fireEvent.keyDown(await screen.findByRole("button", { name: "Episode options" }), { key: "Enter" });
    fireEvent.click(await screen.findByRole("menuitem", { name: "Mark as read" }));
    await waitFor(() =>
      expect(request).toHaveBeenCalledWith("PATCH", `/api/podcasts/episodes/${unread.id}`, { read: true }),
    );
  });

  it("marks a read episode as unread again and shows an empty unread view", async () => {
    const read = episode({ status: "completed", transcriptId: "d".repeat(32), readAt: "2026-09-08T10:00:00Z" });
    request.mockImplementation(async (_method: string, path: string) => ({
      json: async () =>
        path.includes("view=unread")
          ? { items: [], total: 0 }
          : path.includes("/episodes?")
            ? { items: [read], total: 1 }
            : { ok: true },
    }));
    mount([subscription()]);
    const row = (await screen.findByText("Understanding tomorrow")).closest("article");
    expect(within(row!).getByText("Read")).toBeVisible();
    expect(screen.queryByRole("button", { name: "Mark all as read" })).toBeNull();
    fireEvent.keyDown(screen.getByRole("button", { name: "Episode options" }), { key: "Enter" });
    fireEvent.click(await screen.findByRole("menuitem", { name: "Mark as unread" }));
    await waitFor(() =>
      expect(request).toHaveBeenCalledWith("PATCH", `/api/podcasts/episodes/${read.id}`, {
        read: false,
        played: false,
      }),
    );
    fireEvent.click(screen.getByRole("button", { name: "Unread" }));
    expect(await screen.findByText(/You're all caught up/)).toBeVisible();
  });

  it("counts playing the downloaded audio as listening once", async () => {
    const ready = episode({ status: "completed", transcriptId: "d".repeat(32), downloadedBytes: 500, isUnread: true });
    request.mockImplementation(async (_method: string, path: string) => ({
      json: async () => (path.includes("/episodes?") ? { items: [ready], total: 1 } : { ok: true }),
    }));
    const { container } = mount([subscription({ unreadCount: 1, attentionCount: 1 })]);
    fireEvent.click(await screen.findByRole("button", { name: "Listen" }));
    const audio = container.querySelector("audio")!;
    fireEvent.play(audio);
    await waitFor(() =>
      expect(request).toHaveBeenCalledWith("PATCH", `/api/podcasts/episodes/${ready.id}`, { played: true }),
    );
  });

  it("keeps an episode and its player open after listening moved it out of the unread view", async () => {
    const ready = episode({ status: "completed", transcriptId: "d".repeat(32), downloadedBytes: 500, isUnread: true });
    let played = false;
    request.mockImplementation(async (method: string, path: string) => {
      if (method === "PATCH") played = true;
      return {
        json: async () =>
          path.includes("view=unread")
            ? { items: played ? [] : [ready], total: played ? 0 : 1 }
            : path.includes("/episodes?")
              ? { items: [ready], total: 1 }
              : { ok: true },
      };
    });
    const { container } = mount([subscription({ unreadCount: 1, attentionCount: 1 })]);
    fireEvent.click(await screen.findByRole("button", { name: "Unread (1)" }));
    fireEvent.click(await screen.findByRole("button", { name: "Listen" }));
    fireEvent.play(container.querySelector("audio")!);
    await waitFor(() =>
      expect(request).toHaveBeenCalledWith(
        "GET",
        `/api/podcasts/subscriptions/${"a".repeat(32)}/episodes?offset=0&view=unread`,
      ),
    );
    await waitFor(() =>
      expect(
        request.mock.calls.filter(([method, path]) => method === "GET" && String(path).includes("view=unread")),
      ).toHaveLength(2),
    );
    const row = (await screen.findByText("Listened")).closest("article");
    expect(within(row!).getByText("Understanding tomorrow")).toBeVisible();
    expect(container.querySelector("audio")).not.toBeNull();
    expect(request.mock.calls.filter(([method]) => method === "PATCH")).toHaveLength(1);

    fireEvent.click(screen.getByRole("button", { name: "Close player" }));
    expect(await screen.findByText(/You're all caught up/)).toBeVisible();
  });

  it("marks a whole podcast as read", async () => {
    const sub = subscription({ newCount: 1, unreadCount: 1, attentionCount: 2 });
    request.mockImplementation(async (_method: string, path: string) => ({
      json: async () => (path.includes("/episodes?") ? { items: [], total: 0 } : { updated: 2 }),
    }));
    mount([sub]);
    fireEvent.click(await screen.findByRole("button", { name: "Mark all as read" }));
    await waitFor(() =>
      expect(request).toHaveBeenCalledWith("POST", `/api/podcasts/subscriptions/${sub.id}/read`, undefined, {
        timeoutMs: 60_000,
      }),
    );
  });

  it("opens the podcast named by a notification link and consumes the request", async () => {
    const first = subscription();
    const second = subscription({ id: "e".repeat(32), title: "Second show", newCount: 1, attentionCount: 1 });
    window.history.replaceState(null, "", `/podcasts?subscription=${second.id}`);
    mount([first, second]);
    expect(await screen.findByRole("heading", { name: "Second show", level: 2 })).toBeVisible();
    await waitFor(() => expect(window.location.search).toBe(""));
    expect(window.location.pathname).toBe("/podcasts");
    expect(screen.getByRole("button", { name: /Second show/ })).toHaveAttribute("aria-current", "true");
  });
});
