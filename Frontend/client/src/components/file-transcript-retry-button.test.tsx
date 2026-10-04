import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { LocaleProvider, LANGUAGE_STORAGE_KEY } from "@/i18n";
import { FileTranscriptRetryButton } from "./file-transcript-retry-button";
import { fetchWithTimeout } from "@/lib/fetch-with-timeout";
import { startFileUploadBatch } from "@/lib/file-upload-store";

const { navigate, toast } = vi.hoisted(() => ({ navigate: vi.fn(), toast: vi.fn() }));

vi.mock("@/lib/fetch-with-timeout", () => ({ fetchWithTimeout: vi.fn() }));
vi.mock("@/lib/backend", () => ({ apiUrl: (path: string) => path }));
vi.mock("@/hooks/use-toast", () => ({ useToast: () => ({ toast }) }));
vi.mock("@/lib/file-upload-store", () => ({ startFileUploadBatch: vi.fn() }));
vi.mock("wouter", () => ({ useLocation: () => ["/file", navigate] }));

function mount(props: { resumeAvailable?: boolean } = { resumeAvailable: false }, cachedEligibility?: boolean) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  if (cachedEligibility !== undefined) {
    client.setQueryData(["/api/transcripts", "a".repeat(32)], { resumeAvailable: cachedEligibility });
  }
  return render(
    <QueryClientProvider client={client}>
      <LocaleProvider>
        <div onClick={() => navigate("old-record")}>
          <FileTranscriptRetryButton transcriptId={"a".repeat(32)} {...props} />
        </div>
      </LocaleProvider>
    </QueryClientProvider>,
  );
}

describe("File transcript retry", () => {
  beforeEach(() => {
    window.localStorage.setItem(LANGUAGE_STORAGE_KEY, "en");
    window.history.replaceState({}, "", "/file");
    vi.mocked(fetchWithTimeout).mockReset();
    vi.mocked(fetchWithTimeout).mockImplementation(
      async () => new Response(JSON.stringify({ resumeAvailable: false })),
    );
    vi.mocked(startFileUploadBatch).mockReset();
  });
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
    const button = screen.getByRole("button", { name: "Retry transcription" });
    await waitFor(() => expect(button).toBeEnabled());
    fireEvent.click(button);
    fireEvent.click(button);
    await waitFor(() => expect(fetch).toHaveBeenCalledTimes(2));
    expect(fetch.mock.calls[1][0]).toBe(`/api/podcasts/episodes/${"b".repeat(32)}/queue`);
    expect(fetch.mock.calls[1][1]?.method).toBe("POST");
    finish(new Response("{}", { status: 202 }));
    expect(await screen.findByRole("button", { name: "Queued" })).toBeDisabled();
  });

  it("keeps history mounting free of detail and podcast requests until explicit recovery", () => {
    mount({});
    expect(screen.getByRole("button", { name: "Retry transcription" })).toBeEnabled();
    expect(fetchWithTimeout).not.toHaveBeenCalled();
  });

  it("opens file selection for an ordinary upload and admits one new attempt without opening the old card", async () => {
    vi.mocked(fetchWithTimeout).mockResolvedValueOnce(new Response(JSON.stringify({ episode: null })));
    vi.mocked(startFileUploadBatch).mockResolvedValue({
      failures: [],
      responses: [
        { id: "new-file", title: "recording.mp4", type: "file", status: "processing", date: "", duration: "" },
      ],
    });
    mount();
    const button = screen.getByRole("button", { name: "Retry transcription" });
    await waitFor(() => expect(button).toBeEnabled());
    fireEvent.click(button);
    expect(await screen.findByRole("dialog")).toBeVisible();
    expect(navigate).not.toHaveBeenCalled();
    const file = new File(["fixture"], "recording.mp4", { type: "video/mp4" });
    fireEvent.change(screen.getByLabelText("Original audio or video file"), { target: { files: [file] } });
    await waitFor(() => expect(navigate).toHaveBeenCalledWith("/transcript/new-file"));
    expect(startFileUploadBatch).toHaveBeenCalledExactlyOnceWith([file], {
      getServerProcessingText: expect.any(Function),
    });
    expect(fetchWithTimeout).toHaveBeenCalledTimes(2);
  });

  it("allows the same file to be selected again after an upload failure", async () => {
    vi.mocked(fetchWithTimeout).mockResolvedValueOnce(new Response(JSON.stringify({ episode: null })));
    vi.mocked(startFileUploadBatch).mockResolvedValue({
      failures: [{ fileName: "recording.mp4", error: "Upload failed" }],
      responses: [],
    });
    mount();
    const button = screen.getByRole("button", { name: "Retry transcription" });
    await waitFor(() => expect(button).toBeEnabled());
    fireEvent.click(button);
    await screen.findByRole("dialog");
    const file = new File(["fixture"], "recording.mp4");
    const input = screen.getByLabelText("Original audio or video file");
    fireEvent.change(input, { target: { files: [file] } });
    await waitFor(() => expect(screen.getByRole("button", { name: "Choose file" })).toBeEnabled());
    fireEvent.change(input, { target: { files: [file] } });
    await waitFor(() => expect(startFileUploadBatch).toHaveBeenCalledTimes(2));
    expect(navigate).not.toHaveBeenCalled();
  });

  it.each([
    ["a".repeat(32), `Upload failed\nReference: ${"a".repeat(32)}`],
    ["not-a-valid-request-reference", "Upload failed"],
  ])(
    "preserves only a valid upload failure correlation in the retry toast (%s)",
    async (correlationId, description) => {
      toast.mockClear();
      vi.mocked(fetchWithTimeout).mockResolvedValueOnce(new Response(JSON.stringify({ episode: null })));
      vi.mocked(startFileUploadBatch).mockResolvedValue({
        failures: [{ fileName: "recording.mp4", error: "Upload failed", correlationId }],
        responses: [],
      });
      mount();
      fireEvent.click(screen.getByRole("button", { name: "Retry transcription" }));
      await screen.findByRole("dialog");
      fireEvent.change(screen.getByLabelText("Original audio or video file"), {
        target: { files: [new File(["fixture"], "recording.mp4")] },
      });
      await waitFor(() =>
        expect(toast).toHaveBeenCalledWith({ title: "Retry failed", description, variant: "destructive" }),
      );
    },
  );

  it("does not assume an uploaded file when the podcast lookup fails", async () => {
    vi.mocked(fetchWithTimeout).mockRejectedValue(new Error("Network unavailable"));
    mount();
    const button = screen.getByRole("button", { name: "Retry transcription" });
    await waitFor(() => expect(button).toBeEnabled());
    fireEvent.click(button);
    await waitFor(() => expect(toast).toHaveBeenCalledWith(expect.objectContaining({ title: "Retry failed" })));
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(startFileUploadBatch).not.toHaveBeenCalled();
    expect(button).toBeEnabled();
  });

  it("does not offer a fresh upload when checkpoint eligibility cannot be checked", async () => {
    vi.mocked(fetchWithTimeout).mockImplementation(async (url) => {
      if (String(url).startsWith("/api/podcasts/")) return new Response(JSON.stringify({ episode: null }));
      throw new Error("Network unavailable");
    });
    mount({});
    const button = screen.getByRole("button", { name: "Retry transcription" });
    await waitFor(() => expect(button).toBeEnabled());
    fireEvent.click(button);
    await waitFor(() => expect(toast).toHaveBeenCalledWith(expect.objectContaining({ title: "Retry failed" })));
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(startFileUploadBatch).not.toHaveBeenCalled();
  });

  it("rechecks cached negative eligibility before offering a new paid attempt", async () => {
    vi.mocked(fetchWithTimeout).mockImplementation(
      async (url) =>
        new Response(
          JSON.stringify(String(url).startsWith("/api/podcasts/") ? { episode: null } : { resumeAvailable: true }),
        ),
    );
    mount({}, false);
    const button = screen.getByRole("button", { name: "Retry transcription" });
    await waitFor(() => expect(button).toBeEnabled());
    fireEvent.click(button);
    expect(await screen.findByRole("button", { name: "Resume transcription" })).toBeEnabled();
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(startFileUploadBatch).not.toHaveBeenCalled();
  });

  it("does not trust cached negative eligibility after its recheck fails", async () => {
    vi.mocked(fetchWithTimeout).mockImplementation(async (url) => {
      if (String(url).startsWith("/api/podcasts/")) return new Response(JSON.stringify({ episode: null }));
      throw new Error("Network unavailable");
    });
    mount({}, false);
    const button = screen.getByRole("button", { name: "Retry transcription" });
    await waitFor(() => expect(button).toBeEnabled());
    fireEvent.click(button);
    await waitFor(() => expect(toast).toHaveBeenCalledWith(expect.objectContaining({ title: "Retry failed" })));
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(startFileUploadBatch).not.toHaveBeenCalled();
  });

  it("rechecks stale positive eligibility instead of trapping a failed history card on Resume", async () => {
    vi.mocked(fetchWithTimeout)
      .mockResolvedValueOnce(new Response(JSON.stringify({ resumeAvailable: false })))
      .mockResolvedValueOnce(new Response(JSON.stringify({ episode: null })));
    mount({}, true);
    expect(fetchWithTimeout).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "Retry transcription" }));
    expect(await screen.findByRole("dialog")).toBeVisible();
    expect(screen.queryByRole("button", { name: "Resume transcription" })).not.toBeInTheDocument();
  });

  it("rechecks eligibility after a rejected Resume before offering recovery again", async () => {
    vi.mocked(fetchWithTimeout)
      .mockResolvedValueOnce(new Response(JSON.stringify({ resumeAvailable: true })))
      .mockResolvedValueOnce(new Response(JSON.stringify({ error: "Checkpoint no longer resumable" }), { status: 409 }))
      .mockResolvedValueOnce(new Response(JSON.stringify({ resumeAvailable: false })))
      .mockResolvedValueOnce(new Response(JSON.stringify({ episode: null })));
    mount({});
    fireEvent.click(screen.getByRole("button", { name: "Retry transcription" }));
    fireEvent.click(await screen.findByRole("button", { name: "Resume transcription" }));
    fireEvent.click(await screen.findByRole("button", { name: "Retry transcription" }));
    expect(await screen.findByRole("dialog")).toBeVisible();
    expect(startFileUploadBatch).not.toHaveBeenCalled();
    expect(navigate).not.toHaveBeenCalled();
  });

  it("rechecks eligibility after file selection before admitting another upload", async () => {
    vi.mocked(fetchWithTimeout)
      .mockResolvedValueOnce(new Response(JSON.stringify({ episode: null })))
      .mockResolvedValueOnce(new Response(JSON.stringify({ resumeAvailable: true })));
    mount();
    const button = screen.getByRole("button", { name: "Retry transcription" });
    await waitFor(() => expect(button).toBeEnabled());
    fireEvent.click(button);
    await screen.findByRole("dialog");
    fireEvent.change(screen.getByLabelText("Original audio or video file"), {
      target: { files: [new File(["fixture"], "recording.mp4")] },
    });
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
    expect(startFileUploadBatch).not.toHaveBeenCalled();
    expect(navigate).not.toHaveBeenCalled();
  });

  it("does not move the user back to a transcript after they leave during upload", async () => {
    vi.mocked(fetchWithTimeout).mockResolvedValueOnce(new Response(JSON.stringify({ episode: null })));
    let finish!: (result: Awaited<ReturnType<typeof startFileUploadBatch>>) => void;
    vi.mocked(startFileUploadBatch).mockReturnValue(
      new Promise((resolve) => {
        finish = resolve;
      }),
    );
    mount();
    const button = screen.getByRole("button", { name: "Retry transcription" });
    await waitFor(() => expect(button).toBeEnabled());
    fireEvent.click(button);
    await screen.findByRole("dialog");
    fireEvent.change(screen.getByLabelText("Original audio or video file"), {
      target: { files: [new File(["fixture"], "recording.mp4")] },
    });
    window.history.pushState({}, "", "/settings");
    finish({
      failures: [],
      responses: [
        { id: "new-file", title: "recording.mp4", type: "file", status: "processing", date: "", duration: "" },
      ],
    });
    await waitFor(() => expect(toast).toHaveBeenCalledWith(expect.objectContaining({ title: "Retry started" })));
    expect(navigate).not.toHaveBeenCalled();
  });
});
