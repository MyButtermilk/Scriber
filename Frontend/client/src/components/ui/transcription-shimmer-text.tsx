import { memo } from "react";

import { cn } from "@/lib/utils";

type TranscriptionShimmerTone = "blue" | "energy";

interface TranscriptionShimmerTextProps {
  children: string;
  className?: string;
  tone?: TranscriptionShimmerTone;
}

export const TranscriptionShimmerText = memo(function TranscriptionShimmerText({
  children,
  className,
  tone = "energy",
}: TranscriptionShimmerTextProps) {
  return (
    <span
      className={cn(
        "transcription-shimmer-text",
        tone === "energy" ? "transcription-shimmer-text--energy" : "transcription-shimmer-text--blue",
        className,
      )}
      data-testid="transcription-shimmer-text"
    >
      {children}
    </span>
  );
});
