import wasmUrl from "@formepdf/core/pkg-web/forme_bg.wasm?url";

export interface PdfEngine {
  renderSerializedDoc(document: Record<string, unknown>): Promise<Uint8Array>;
}

let enginePromise: Promise<PdfEngine> | null = null;

async function loadEngine(): Promise<PdfEngine> {
  // The worker entry is the one that instantiates the layout engine from an
  // explicit module, so Vite needs no WebAssembly plugin: the binary is a
  // hashed same-origin asset that is only fetched on the first PDF export.
  const core = await import("@formepdf/core/worker");
  const response = await fetch(wasmUrl);
  if (!response.ok) throw new Error(`PDF engine is unavailable (${response.status})`);
  await core.init(await response.arrayBuffer());
  return { renderSerializedDoc: (document) => core.renderSerializedDoc(document) };
}

/** Instantiate the PDF layout engine once; a failed load can be retried. */
export function loadPdfEngine(): Promise<PdfEngine> {
  if (!enginePromise) {
    enginePromise = loadEngine().catch((error) => {
      enginePromise = null;
      throw error;
    });
  }
  return enginePromise;
}
