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
  correlationHeader: string | null = null;
  open() {}
  getResponseHeader(name: string) {
    return name.toLowerCase() === "x-scriber-correlation-id" ? this.correlationHeader : null;
  }
  send() {
    UploadRequest.requests.push(this);
  }
  finish(id: string, status = 200, correlationId?: unknown) {
    this.status = status;
    this.responseText = JSON.stringify(
      status === 200 ? { id, status: "processing" } : { message: "Upload failed", correlationId },
    );
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
  it("publishes only changed progress and still enters server processing after rounded 100%", async () => {
    vi.stubGlobal("XMLHttpRequest", UploadRequest);
    const { startFileUploadBatch, getFileUploadSnapshot, subscribeFileUpload } = await import("./file-upload-store");
    const batch = startFileUploadBatch(files("long.wav"), options);
    const changed = vi.fn();
    const stop = subscribeFileUpload(changed);
    const request = UploadRequest.requests[0];
    const progress = (loaded: number) => request.upload.onprogress?.({ lengthComputable: true, loaded, total: 10_000 });
    const initial = getFileUploadSnapshot();
    for (let value = 1; value < 50; value++) progress(value);
    expect(getFileUploadSnapshot()).toBe(initial);
    expect(changed).not.toHaveBeenCalled();
    for (let value = 50; value < 10_000; value++) progress(value);
    expect(changed).toHaveBeenCalledTimes(100);
    expect(getFileUploadSnapshot()).toMatchObject({ status: "uploading", progress: 100 });
    progress(10_000);
    expect(changed).toHaveBeenCalledTimes(101);
    expect(getFileUploadSnapshot()).toMatchObject({ status: "server_processing", progress: 100 });
    request.upload.onload?.();
    expect(changed).toHaveBeenCalledTimes(101);
    request.finish("finished");
    expect((await batch).responses).toEqual([{ id: "finished", status: "processing" }]);
    expect(getFileUploadSnapshot().status).toBe("completed");
    stop();
    await settle();
  });

  it.each(["body", "header"])(
    "keeps the server reference from the %s separate from its translated message",
    async (source) => {
      vi.stubGlobal("XMLHttpRequest", UploadRequest);
      const { startFileUploadBatch, getFileUploadSnapshot } = await import("./file-upload-store");
      const correlationId = "0123456789abcdef0123456789abcdef";
      const batch = startFileUploadBatch(files("failed.wav"), options);
      const request = UploadRequest.requests[0];
      if (source === "header") request.correlationHeader = correlationId;
      request.finish("unused", 500, source === "body" ? correlationId : undefined);
      const result = await batch;
      expect(result.failures[0].correlationId).toBe(correlationId);
      expect(result.failures[0].error).not.toContain(correlationId);
      expect(getFileUploadSnapshot().items[0]).toMatchObject({ status: "failed", correlationId });
      await settle();
    },
  );

  it("discards malformed server references", async () => {
    vi.stubGlobal("XMLHttpRequest", UploadRequest);
    const { startFileUploadBatch, getFileUploadSnapshot } = await import("./file-upload-store");
    const batch = startFileUploadBatch(files("failed.wav"), options);
    UploadRequest.requests[0].correlationHeader = "private-file.wav";
    UploadRequest.requests[0].finish("unused", 500, "token=private-secret");
    expect((await batch).failures[0].correlationId).toBeUndefined();
    expect(getFileUploadSnapshot().items[0].correlationId).toBeUndefined();
    await settle();
  });

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
