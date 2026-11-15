import { serialize } from "@formepdf/react";
import type { ReactElement } from "react";

import type { MeetingExportDocument, TranscriptExportDocument } from "@/lib/api-types";

import { meetingPdfElement, transcriptPdfElement } from "./documents";
import type { TranscriptPdfLabels, TranscriptPdfVariant } from "./documents";
import { loadPdfEngine } from "./engine";
import { loadScriberPdfFonts } from "./fonts";

export type { TranscriptPdfLabels, TranscriptPdfVariant } from "./documents";

async function render(element: (fonts: Awaited<ReturnType<typeof loadScriberPdfFonts>>) => ReactElement) {
  const [engine, fonts] = await Promise.all([loadPdfEngine(), loadScriberPdfFonts()]);
  const document = serialize(element(fonts)) as unknown as Record<string, unknown>;
  return engine.renderSerializedDoc(document);
}

/** Typeset a transcript, or only its summary, as PDF bytes in the WebView. */
export function renderTranscriptPdf(
  model: TranscriptExportDocument,
  labels: TranscriptPdfLabels,
  variant: TranscriptPdfVariant,
): Promise<Uint8Array> {
  return render((fonts) => transcriptPdfElement({ model, labels, variant, fonts }));
}

/** Typeset a Meeting export as PDF bytes in the WebView. */
export function renderMeetingPdf(model: MeetingExportDocument): Promise<Uint8Array> {
  return render((fonts) => meetingPdfElement({ model, fonts }));
}
