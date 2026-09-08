import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { describe, expect, it, vi } from "vitest";
import { LocaleProvider } from "@/i18n";
import { PodcastTranscriptRetryButton } from "./podcast-transcript-retry-button";
import { fetchWithTimeout } from "@/lib/fetch-with-timeout";

vi.mock("@/lib/fetch-with-timeout", () => ({ fetchWithTimeout: vi.fn() }));
vi.mock("@/lib/backend", () => ({ apiUrl: (path: string) => path }));
vi.mock("@/hooks/use-toast", () => ({ useToast: () => ({ toast: vi.fn() }) }));

function mount() {
  return render(
    <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
      <LocaleProvider>
        <PodcastTranscriptRetryButton transcriptId={"a".repeat(32)} />
      </LocaleProvider>
    </QueryClientProvider>,
  );
}

describe("Podcast transcript retry", () => {
  it("queues the exact linked episode once and disables repeated clicks", async () => {
    const fetch = vi.mocked(fetchWithTimeout);
    fetch.mockResolvedValueOnce(new Response(JSON.stringify({ episode: { id: "b".repeat(32), status: "failed" } })));
    let finish!: (response: Response) => void;
    fetch.mockReturnValueOnce(
      new Promise((resolve) => {
        finish = resolve;
      }),
    );
    mount();
    const button = await screen.findByRole("button", { name: "Retry transcription" });
    fireEvent.click(button);
    fireEvent.click(button);
    expect(fetch).toHaveBeenCalledTimes(2);
    expect(fetch.mock.calls[1][0]).toBe(`/api/podcasts/episodes/${"b".repeat(32)}/queue`);
    expect(fetch.mock.calls[1][1]?.method).toBe("POST");
    finish(new Response("{}", { status: 202 }));
    expect(await screen.findByRole("button", { name: "Queued" })).toBeDisabled();
  });

  it("does not offer podcast retry for an ordinary uploaded file", async () => {
    vi.mocked(fetchWithTimeout).mockResolvedValueOnce(new Response(JSON.stringify({ episode: null })));
    const view = mount();
    await waitFor(() => expect(fetchWithTimeout).toHaveBeenCalledTimes(1));
    expect(view.queryByRole("button")).toBeNull();
  });
});
