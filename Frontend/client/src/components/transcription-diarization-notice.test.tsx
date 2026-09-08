import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { LocaleProvider } from "@/i18n";
import { TranscriptionDiarizationNotice } from "./transcription-diarization-notice";

describe("Diarization recovery notice", () => {
  it("explains a text-only recovery without adding text to the transcript", () => {
    render(
      <LocaleProvider>
        <TranscriptionDiarizationNotice code="diarization_unavailable" />
      </LocaleProvider>,
    );
    expect(screen.getByRole("status")).toHaveTextContent("without speaker labels");
  });
  it("does not report a fallback for ordinary results", () => {
    const view = render(
      <LocaleProvider>
        <TranscriptionDiarizationNotice />
      </LocaleProvider>,
    );
    expect(view.queryByRole("status")).toBeNull();
  });
});
