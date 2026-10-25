import { act, fireEvent, render, screen } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type { ReactElement, ReactNode } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { Router } from "wouter";
import { memoryLocation } from "wouter/memory-location";
import type { ScriberWebSocketMessage } from "@/contexts/WebSocketContext";
import { LANGUAGE_STORAGE_KEY, LocaleProvider } from "@/i18n";
import { PodcastNotificationBridge } from "./PodcastNotificationBridge";

const { socket, toast } = vi.hoisted(() => ({
  socket: { listener: null as ((message: ScriberWebSocketMessage) => void) | null },
  toast: vi.fn(),
}));
vi.mock("@/contexts/WebSocketContext", () => ({
  useSharedWebSocket: (listener: (message: ScriberWebSocketMessage) => void) => {
    socket.listener = listener;
    return { isConnected: true };
  },
}));
vi.mock("@/hooks/use-toast", () => ({ useToast: () => ({ toast }) }));

type ToastOptions = { title: ReactNode; description: ReactNode; action: ReactElement };

function mount() {
  const location = memoryLocation({ path: "/live-mic", record: true });
  const client = new QueryClient();
  const invalidate = vi.spyOn(client, "invalidateQueries");
  render(
    <QueryClientProvider client={client}>
      <Router hook={location.hook}>
        <LocaleProvider>
          <PodcastNotificationBridge />
        </LocaleProvider>
      </Router>
    </QueryClientProvider>,
  );
  return { location, invalidate };
}

function emit(message: ScriberWebSocketMessage) {
  act(() => socket.listener?.(message));
}

function lastToast(): ToastOptions {
  return toast.mock.calls.at(-1)?.[0] as ToastOptions;
}

function renderToast(options: ToastOptions) {
  return render(
    <div>
      <p>{options.title}</p>
      <p>{options.description}</p>
      {options.action}
    </div>,
  );
}

describe("PodcastNotificationBridge", () => {
  beforeEach(() => {
    window.localStorage.setItem(LANGUAGE_STORAGE_KEY, "en");
    socket.listener = null;
    toast.mockReset();
  });

  it("announces a readable episode once and opens its transcript", () => {
    const { location, invalidate } = mount();
    const ready: ScriberWebSocketMessage = {
      apiVersion: "1",
      type: "podcast_episode_ready",
      eventId: "ready-1",
      episodeId: "b".repeat(32),
      subscriptionId: "a".repeat(32),
      transcriptId: "c".repeat(32),
      title: "Understanding tomorrow",
      podcastTitle: "Thoughtful conversations",
    };
    emit(ready);
    emit(ready);
    expect(toast).toHaveBeenCalledTimes(1);
    expect(invalidate).toHaveBeenCalledWith({ queryKey: ["/api/podcasts"] });
    expect(invalidate).toHaveBeenCalledWith({ queryKey: ["/api/podcasts/episodes"] });

    renderToast(lastToast());
    expect(screen.getByText("Episode ready to read")).toBeVisible();
    expect(screen.getByText("Thoughtful conversations: Understanding tomorrow")).toBeVisible();
    fireEvent.click(screen.getByRole("button", { name: "Read now" }));
    expect(location.history?.at(-1)).toBe(`/transcript/${"c".repeat(32)}`);
  });

  it("links one podcast's new episodes to that podcast", () => {
    const { location } = mount();
    emit({
      apiVersion: "1",
      type: "podcast_episodes_discovered",
      eventId: "found-1",
      count: 2,
      queuedCount: 2,
      podcasts: [{ subscriptionId: "a".repeat(32), title: "Thoughtful conversations", count: 2 }],
    });
    renderToast(lastToast());
    expect(screen.getByText("2 new podcast episodes")).toBeVisible();
    expect(screen.getByText("Thoughtful conversations · Transcripts and summaries are being prepared.")).toBeVisible();
    fireEvent.click(screen.getByRole("button", { name: "Show" }));
    expect(location.history?.at(-1)).toBe(`/podcasts?subscription=${"a".repeat(32)}`);
  });

  it("summarizes several podcasts and opens the library", () => {
    const { location } = mount();
    emit({
      apiVersion: "1",
      type: "podcast_episodes_discovered",
      eventId: "found-2",
      count: 1,
      queuedCount: 0,
      podcasts: [
        { subscriptionId: "a".repeat(32), title: "First show", count: 1 },
        { subscriptionId: "e".repeat(32), title: "Second show", count: 1 },
      ],
    });
    renderToast(lastToast());
    expect(screen.getByText("New podcast episode")).toBeVisible();
    expect(screen.getByText("First show, Second show")).toBeVisible();
    fireEvent.click(screen.getByRole("button", { name: "Show" }));
    expect(location.history?.at(-1)).toBe("/podcasts");
  });

  it("ignores unrelated events", () => {
    const { invalidate } = mount();
    emit({ apiVersion: "1", type: "history_updated" } as ScriberWebSocketMessage);
    expect(toast).not.toHaveBeenCalled();
    expect(invalidate).not.toHaveBeenCalled();
  });
});
