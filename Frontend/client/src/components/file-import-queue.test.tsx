import { render, screen } from "@testing-library/react";
import { expect, it } from "vitest";
import { LocaleProvider } from "@/i18n";
import type { FileUploadQueueItem } from "@/lib/file-upload-store";
import { FileImportQueue } from "./file-import-queue";

it("separates measured upload progress from audio preparation and shows failures", () => {
  const item: FileUploadQueueItem = {
    id: "a",
    fileName: "video.mp4",
    status: "uploading",
    progress: 47,
    statusText: "Uploading…",
    response: null,
    error: "",
  };
  render(
    <LocaleProvider>
      <FileImportQueue
        items={[
          item,
          {
            ...item,
            id: "b",
            fileName: "preparing.mp4",
            status: "server_processing",
            progress: 100,
            statusText: "Extracting audio…",
          },
          { ...item, id: "c", fileName: "failed.wav", status: "failed", error: "Upload failed" },
          { ...item, id: "d", fileName: "finished.wav", status: "completed" },
        ]}
      />
    </LocaleProvider>,
  );
  const bars = screen.getAllByRole("progressbar");
  expect(bars).toHaveLength(2);
  expect(bars[0]).toHaveAttribute("aria-valuenow", "47");
  expect(bars[1]).not.toHaveAttribute("aria-valuenow");
  expect(screen.getByRole("alert")).not.toBeEmptyDOMElement();
  expect(screen.queryByText("finished.wav")).toBeNull();
});
