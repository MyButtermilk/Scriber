import { beforeEach, describe, expect, it, vi } from "vitest";

import type { TranscriptPdfLabels } from "@/lib/pdf-export";

const invoke = vi.fn();
const renderTranscriptPdf = vi.fn();
const renderMeetingPdf = vi.fn();
const fetchWithTimeout = vi.fn();

vi.mock("@tauri-apps/api/core", () => ({ invoke: (...args: unknown[]) => invoke(...args) }));
vi.mock("@/lib/backend", () => ({ apiUrl: (path: string) => `http://backend${path}`, isTauriRuntime: () => true }));
vi.mock("@/lib/fetch-with-timeout", () => ({
  fetchWithTimeout: (...args: unknown[]) => fetchWithTimeout(...args),
}));
vi.mock("@/lib/pdf-export", () => ({
  renderTranscriptPdf: (...args: unknown[]) => renderTranscriptPdf(...args),
  renderMeetingPdf: (...args: unknown[]) => renderMeetingPdf(...args),
}));

import { saveMeetingExport } from "./meeting-export";
import { saveTranscriptPdfExport } from "./transcript-export";

const labels: TranscriptPdfLabels = {
  locale: "de",
  transcript: "Transkript",
  summary: "Zusammenfassung",
  date: "Datum",
  duration: "Dauer",
  pageOf: "Seite {page} von {total}",
  speaker: (speakerNumber) => `Sprecher ${speakerNumber}`,
};

const transcriptModel = {
  apiVersion: "1",
  kind: "transcript",
  title: "Weekly sync",
  date: "2026-08-14",
  duration: "00:10",
  content: "hello world",
  summary: "<section><h2>Title</h2><p>Standfirst</p></section>",
  summaryFormat: "html",
  filenameStem: "Weekly sync",
};

const meetingModel = {
  apiVersion: "1",
  kind: "meeting",
  language: "en",
  title: "Roadmap",
  labels: {},
  sections: [],
  notes: [],
  segments: [],
  filename: "Roadmap.pdf",
};

const PDF_BYTES = new Uint8Array([0x25, 0x50, 0x44, 0x46]);

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
}

function backendPdfResponse(filename: string): Response {
  return new Response(new Uint8Array([1, 2, 3]), {
    status: 200,
    headers: { "Content-Disposition": `attachment; filename="${filename}"` },
  });
}

function requestedPaths(): string[] {
  return fetchWithTimeout.mock.calls.map(([url]) => String(url).replace("http://backend", ""));
}

beforeEach(() => {
  invoke.mockReset();
  renderTranscriptPdf.mockReset();
  renderMeetingPdf.mockReset();
  fetchWithTimeout.mockReset();
  invoke.mockImplementation(async (_command: string, args: { filename: string }) => ({
    token: "token",
    path: `C:\\Exports\\${args.filename}`,
    directory: "C:\\Exports",
    filename: args.filename,
  }));
});

describe("transcript PDF export", () => {
  it("typesets the structured document in the WebView and saves those bytes", async () => {
    fetchWithTimeout.mockResolvedValueOnce(jsonResponse(transcriptModel));
    renderTranscriptPdf.mockResolvedValueOnce(PDF_BYTES);

    const result = await saveTranscriptPdfExport({
      transcriptId: "t-1",
      variant: "full",
      labels,
      fallbackName: "Weekly sync.pdf",
    });

    expect(requestedPaths()).toEqual(["/api/transcripts/t-1/export/document"]);
    expect(renderTranscriptPdf).toHaveBeenCalledWith(transcriptModel, labels, "full");
    expect(invoke).toHaveBeenCalledTimes(1);
    const [command, args] = invoke.mock.calls[0] as [
      string,
      { filename: string; extension: string; bytes: Uint8Array },
    ];
    expect(command).toBe("save_transcript_export");
    expect(args.filename).toBe("Weekly sync.pdf");
    expect(args.extension).toBe("pdf");
    expect(Array.from(args.bytes)).toEqual(Array.from(PDF_BYTES));
    expect(result).toEqual({ status: "saved", desktop: true, filename: "Weekly sync.pdf" });
  });

  it("names the summary-only document after the summary", async () => {
    fetchWithTimeout.mockResolvedValueOnce(jsonResponse(transcriptModel));
    renderTranscriptPdf.mockResolvedValueOnce(PDF_BYTES);

    await saveTranscriptPdfExport({ transcriptId: "t-1", variant: "summary", labels, fallbackName: "x.pdf" });

    expect(renderTranscriptPdf).toHaveBeenCalledWith(transcriptModel, labels, "summary");
    expect((invoke.mock.calls[0] as [string, { filename: string }])[1].filename).toBe(
      "Weekly sync - Zusammenfassung.pdf",
    );
  });

  it("falls back to the backend PDF when the full document cannot be typeset", async () => {
    fetchWithTimeout
      .mockResolvedValueOnce(jsonResponse(transcriptModel))
      .mockResolvedValueOnce(backendPdfResponse("Weekly sync.pdf"));
    renderTranscriptPdf.mockRejectedValueOnce(new Error("WebAssembly is unavailable"));

    const result = await saveTranscriptPdfExport({
      transcriptId: "t-1",
      variant: "full",
      labels,
      fallbackName: "Weekly sync.pdf",
    });

    expect(requestedPaths()).toEqual(["/api/transcripts/t-1/export/document", "/api/transcripts/t-1/export/pdf"]);
    const [, args] = invoke.mock.calls[0] as [string, { bytes: Uint8Array }];
    expect(Array.from(args.bytes)).toEqual([1, 2, 3]);
    expect(result.status).toBe("saved");
  });

  it("reports a summary document that cannot be created instead of exporting something else", async () => {
    fetchWithTimeout.mockResolvedValueOnce(jsonResponse({ ...transcriptModel, summary: "  " }));

    await expect(
      saveTranscriptPdfExport({ transcriptId: "t-1", variant: "summary", labels, fallbackName: "x.pdf" }),
    ).rejects.toThrow();

    expect(renderTranscriptPdf).not.toHaveBeenCalled();
    expect(invoke).not.toHaveBeenCalled();
    expect(requestedPaths()).toEqual(["/api/transcripts/t-1/export/document"]);
  });

  it("rejects an unsafe transcript id before any request", async () => {
    await expect(
      saveTranscriptPdfExport({ transcriptId: "../settings", variant: "summary", labels, fallbackName: "x.pdf" }),
    ).rejects.toThrow();
    expect(fetchWithTimeout).not.toHaveBeenCalled();
  });

  it("keeps a cancelled save dialog a cancellation", async () => {
    fetchWithTimeout.mockResolvedValueOnce(jsonResponse(transcriptModel));
    renderTranscriptPdf.mockResolvedValueOnce(PDF_BYTES);
    invoke.mockResolvedValueOnce(null);

    await expect(
      saveTranscriptPdfExport({ transcriptId: "t-1", variant: "full", labels, fallbackName: "x.pdf" }),
    ).resolves.toEqual({ status: "cancelled" });
    expect(fetchWithTimeout).toHaveBeenCalledTimes(1);
  });
});

describe("meeting PDF export", () => {
  it("typesets the Meeting document in the WebView", async () => {
    fetchWithTimeout.mockResolvedValueOnce(jsonResponse(meetingModel));
    renderMeetingPdf.mockResolvedValueOnce(PDF_BYTES);

    const result = await saveMeetingExport("/api/meetings/m-1/export/pdf", "Roadmap.pdf");

    expect(requestedPaths()).toEqual(["/api/meetings/m-1/export/document"]);
    const [command, args] = invoke.mock.calls[0] as [string, { filename: string; bytes: Uint8Array }];
    expect(command).toBe("save_meeting_export");
    expect(args.filename).toBe("Roadmap.pdf");
    expect(Array.from(args.bytes)).toEqual(Array.from(PDF_BYTES));
    expect(result.status).toBe("saved");
  });

  it("downloads the backend PDF when the document cannot be typeset", async () => {
    fetchWithTimeout
      .mockResolvedValueOnce(jsonResponse({ message: "unavailable" }, 500))
      .mockResolvedValueOnce(backendPdfResponse("Roadmap.pdf"));

    await saveMeetingExport("/api/meetings/m-1/export/pdf", "Roadmap.pdf");

    expect(requestedPaths()).toEqual(["/api/meetings/m-1/export/document", "/api/meetings/m-1/export/pdf"]);
    expect(renderMeetingPdf).not.toHaveBeenCalled();
    expect(Array.from((invoke.mock.calls[0] as [string, { bytes: Uint8Array }])[1].bytes)).toEqual([1, 2, 3]);
  });

  it("leaves every other Meeting format and the email draft on the backend path", async () => {
    fetchWithTimeout.mockImplementation(async () => backendPdfResponse("Roadmap.docx"));

    await saveMeetingExport("/api/meetings/m-1/export/docx", "Roadmap.docx");
    await saveMeetingExport("/api/meetings/m-1/export-email?attachment=pdf", "Roadmap.eml");

    expect(requestedPaths()).toEqual([
      "/api/meetings/m-1/export/docx",
      "/api/meetings/m-1/export-email?attachment=pdf",
    ]);
    expect(renderMeetingPdf).not.toHaveBeenCalled();
  });
});
