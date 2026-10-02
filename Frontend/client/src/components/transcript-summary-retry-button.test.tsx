import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { LANGUAGE_STORAGE_KEY, LocaleProvider } from "@/i18n";
import { TranscriptSummaryRetryButton } from "./transcript-summary-retry-button";

const { request, toast } = vi.hoisted(() => ({ request: vi.fn(), toast: vi.fn() }));
vi.mock("@/lib/backend", () => ({ apiUrl: (path: string) => path }));
vi.mock("@/lib/fetch-with-timeout", () => ({ fetchWithTimeout: request }));
vi.mock("@/hooks/use-toast", () => ({ useToast: () => ({ toast }) }));

describe("Summary retry actions", () => {
  beforeEach(() => window.localStorage.setItem(LANGUAGE_STORAGE_KEY, "en"));

  it.each([true, false])(
    "keeps compact=%s retry accessible and refreshes the same transcript after completion",
    async (compact) => {
      let finish!: (response: Response) => void;
      request.mockReturnValue(
        new Promise((resolve) => {
          finish = resolve;
        }),
      );
      const onComplete = vi.fn();
      const navigate = vi.fn();
      render(
        <LocaleProvider>
          <div onClick={navigate}>
            <TranscriptSummaryRetryButton
              transcriptId="record"
              transcriptTitle="Recording"
              compact={compact}
              onComplete={onComplete}
            />
          </div>
        </LocaleProvider>,
      );
      const button = screen.getByRole("button", { name: "Retry summary for Recording" });
      expect(button).toHaveTextContent(compact ? "Retry" : "Retry summary");
      fireEvent.click(button);
      expect(screen.getByRole("button", { name: "Retrying summary for Recording" })).toBeDisabled();
      expect(request).toHaveBeenCalledWith(
        "/api/transcripts/record/summarize",
        { method: "POST", credentials: "include" },
        15 * 60_000,
      );
      expect(navigate).not.toHaveBeenCalled();
      finish(new Response("{}"));
      await waitFor(() => expect(onComplete).toHaveBeenCalledExactlyOnceWith("record"));
      expect(screen.getByRole("button", { name: "Retry summary for Recording" })).toBeEnabled();
    },
  );
});
