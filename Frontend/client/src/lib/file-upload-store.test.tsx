import { afterEach, describe, expect, it, vi } from "vitest";

vi.mock("@/lib/backend", () => ({ apiUrl: (path: string) => path }));

class UploadRequest {
  static requests: UploadRequest[] = [];
  upload: {
    onprogress?: (event: { lengthComputable: boolean; loaded: number; total: number }) => void;
    onload?: () => void;
  } = {};
  onload?: () => void;
  status = 200;
  responseText = "";
  statusText = "";
  open() {}
  send() {
    UploadRequest.requests.push(this);
  }
  finish(id: string, status = 200) {
    this.status = status;
    this.responseText = JSON.stringify(status === 200 ? { id, status: "processing" } : { message: "Upload failed" });
    this.onload?.();
  }
}

const files = (...names: string[]) => names.map((name) => new File(["audio"], name));
const options = { getServerProcessingText: () => ({ key: "Preparing audio…" }) };
const settle = async () => {
  await new Promise((resolve) => setTimeout(resolve, 0));
};

afterEach(() => {
  vi.unstubAllGlobals();
  vi.resetModules();
  UploadRequest.requests = [];
});

describe("file upload queue", () => {
  it("prepares two files concurrently, accepts additions, and isolates failures and progress", async () => {
    vi.stubGlobal("XMLHttpRequest", UploadRequest);
    const { startFileUploadBatch, getFileUploadSnapshot } = await import("./file-upload-store");
    const first = startFileUploadBatch(files("one.wav", "two.wav", "three.wav"), options);
    const added = startFileUploadBatch(files("four.wav"), options);
    // Attach rejection observers even on the broken implementation.
    const outcomes = Promise.allSettled([first, added]);
    await settle();
    expect(UploadRequest.requests).toHaveLength(2);
    expect(getFileUploadSnapshot().items.map((item) => item.status)).toEqual([
      "uploading",
      "uploading",
      "queued",
      "queued",
    ]);
    UploadRequest.requests[1].upload.onprogress?.({ lengthComputable: true, loaded: 50, total: 100 });
    expect(getFileUploadSnapshot().items[0].progress).toBe(0);
    expect(getFileUploadSnapshot().items[1].progress).toBeGreaterThan(0);
    UploadRequest.requests[1].finish("two");
    await settle();
    expect(UploadRequest.requests).toHaveLength(3);
    UploadRequest.requests[0].finish("one", 500);
    await settle();
    expect(UploadRequest.requests).toHaveLength(4);
    UploadRequest.requests[2].finish("three");
    UploadRequest.requests[3].finish("four");
    await outcomes;
    expect(await first).toMatchObject({
      responses: [{ id: "two" }, { id: "three" }],
      failures: [{ fileName: "one.wav" }],
    });
    expect(await added).toMatchObject({ responses: [{ id: "four" }], failures: [] });
    expect(getFileUploadSnapshot()).toMatchObject({
      completedFiles: 3,
      failedFiles: 1,
      totalFiles: 4,
      status: "failed",
    });
  });
});
