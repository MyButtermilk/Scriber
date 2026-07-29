import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { TranscriptionShimmerText } from "./transcription-shimmer-text";

describe("TranscriptionShimmerText", () => {
  it("keeps the status text accessible while applying the energy tone", () => {
    render(<TranscriptionShimmerText>Wird transkribiert …</TranscriptionShimmerText>);

    const text = screen.getByText("Wird transkribiert …");
    expect(text).toHaveClass("transcription-shimmer-text--energy");
    expect(text).toHaveTextContent("Wird transkribiert …");
  });

  it("supports the blue tone used by the classic visualizer", () => {
    render(<TranscriptionShimmerText tone="blue">Transcribing...</TranscriptionShimmerText>);

    expect(screen.getByText("Transcribing...")).toHaveClass("transcription-shimmer-text--blue");
  });
});
