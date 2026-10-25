import { QueryClient } from "@tanstack/react-query";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { markPodcastTranscriptRead, resetPodcastTranscriptReadMarksForTests } from "./podcast-progress";

const { request } = vi.hoisted(() => ({ request: vi.fn() }));
vi.mock("@/lib/queryClient", () => ({ apiRequest: request }));

describe("markPodcastTranscriptRead", () => {
  beforeEach(() => {
    resetPodcastTranscriptReadMarksForTests();
    request.mockReset();
  });

  it("coalesces concurrent read marks and refreshes the podcast library only when it changed", async () => {
    const client = new QueryClient();
    const invalidate = vi.spyOn(client, "invalidateQueries");
    request.mockResolvedValue({ json: async () => ({ updated: true }) });
    const id = "c".repeat(32);

    await Promise.all([markPodcastTranscriptRead(client, id), markPodcastTranscriptRead(client, id)]);

    expect(request).toHaveBeenCalledTimes(1);
    expect(request).toHaveBeenCalledWith("POST", `/api/podcasts/transcripts/${id}/read`);
    expect(invalidate).toHaveBeenCalledWith({ queryKey: ["/api/podcasts"] });

    invalidate.mockClear();
    request.mockResolvedValue({ json: async () => ({ updated: false }) });
    await markPodcastTranscriptRead(client, "d".repeat(32));
    expect(invalidate).not.toHaveBeenCalled();
  });

  it("marks a transcript read again when it is reopened after being marked unread", async () => {
    const client = new QueryClient();
    const invalidate = vi.spyOn(client, "invalidateQueries");
    let isRead = false;
    request.mockImplementation(async () => {
      const updated = !isRead;
      isRead = true;
      return { json: async () => ({ updated }) };
    });
    const id = "c".repeat(32);

    await markPodcastTranscriptRead(client, id);
    expect(isRead).toBe(true);
    isRead = false;
    invalidate.mockClear();

    await markPodcastTranscriptRead(client, id);

    expect(isRead).toBe(true);
    expect(request).toHaveBeenCalledTimes(2);
    expect(invalidate).toHaveBeenCalledWith({ queryKey: ["/api/podcasts"] });
  });

  it("skips transcripts that cannot belong to a podcast and retries after a failure", async () => {
    const client = new QueryClient();
    await markPodcastTranscriptRead(client, "123e4567-e89b-12d3-a456-426614174000");
    expect(request).not.toHaveBeenCalled();

    request.mockRejectedValueOnce(new Error("offline"));
    await markPodcastTranscriptRead(client, "c".repeat(32));
    request.mockResolvedValue({ json: async () => ({ updated: true }) });
    await markPodcastTranscriptRead(client, "c".repeat(32));
    expect(request).toHaveBeenCalledTimes(2);
  });
});
