import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { LANGUAGE_STORAGE_KEY, LocaleProvider } from "@/i18n";
import { FileTranscriptResumeButton } from "./file-transcript-resume-button";
import { fetchWithTimeout } from "@/lib/fetch-with-timeout";

const { toast } = vi.hoisted(() => ({ toast: vi.fn() }));
vi.mock("@/lib/fetch-with-timeout", () => ({ fetchWithTimeout: vi.fn() }));
vi.mock("@/lib/backend", () => ({ apiUrl: (path: string) => path }));
vi.mock("@/hooks/use-toast", () => ({ useToast: () => ({ toast }) }));

function mount() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const invalidate = vi.spyOn(client, "invalidateQueries");
  render(
    <QueryClientProvider client={client}>
      <LocaleProvider>
        <FileTranscriptResumeButton transcriptId="retained-file" />
      </LocaleProvider>
    </QueryClientProvider>,
  );
  return { invalidate };
}

describe("File transcription resume", () => {
  beforeEach(() => {
    window.localStorage.setItem(LANGUAGE_STORAGE_KEY, "en");
  });

  it("resumes the same transcript once and refreshes its durable state", async () => {
    let finish!: (response: Response) => void;
    vi.mocked(fetchWithTimeout).mockReturnValueOnce(
      new Promise((resolve) => {
        finish = resolve;
      }),
    );
    const { invalidate } = mount();
    const button = screen.getByRole("button", { name: "Resume transcription" });
    fireEvent.click(button);
    fireEvent.click(button);
    expect(fetchWithTimeout).toHaveBeenCalledTimes(1);
    expect(fetchWithTimeout).toHaveBeenCalledWith(
      "/api/transcripts/retained-file/resume-file",
      { method: "POST", credentials: "include" },
      60_000,
    );
    finish(new Response(JSON.stringify({ success: true, id: "retained-file" }), { status: 202 }));
    expect(await screen.findByRole("button", { name: "Queued" })).toBeDisabled();
    expect(invalidate).toHaveBeenCalledWith({ queryKey: ["/api/transcripts"] });
  });

  it("shows a refused resume and rechecks eligibility", async () => {
    const reason = "This transcription cannot be safely resumed from its saved progress.";
    vi.mocked(fetchWithTimeout).mockResolvedValueOnce(
      new Response(JSON.stringify({ message: reason }), {
        status: 409,
        headers: { "Content-Type": "application/json" },
      }),
    );
    const { invalidate } = mount();
    fireEvent.click(screen.getByRole("button", { name: "Resume transcription" }));
    await waitFor(() =>
      expect(toast).toHaveBeenCalledWith(
        expect.objectContaining({
          description: reason,
          variant: "destructive",
        }),
      ),
    );
    expect(screen.getByRole("button", { name: "Resume transcription" })).toBeEnabled();
    expect(invalidate).toHaveBeenCalledWith({ queryKey: ["/api/transcripts"] });
  });
});
