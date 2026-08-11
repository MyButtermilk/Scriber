import { invoke } from "@tauri-apps/api/core";
import { apiUrl, isTauriRuntime } from "@/lib/backend";
import { fetchWithTimeout } from "@/lib/fetch-with-timeout";

export interface SavedDesktopSupportBundle {
  status: "saved";
  desktop: true;
  token: string;
  path: string;
  directory: string;
  filename: string;
}

export interface SavedBrowserSupportBundle {
  status: "saved";
  desktop: false;
  filename: string;
}

export type SupportBundleSaveResult =
  | SavedDesktopSupportBundle
  | SavedBrowserSupportBundle
  | { status: "cancelled" };

export function supportBundleFilename(disposition: string | null): string {
  const match = disposition?.match(/filename="?([^";]+)"?/i);
  return match?.[1] || `scriber-support-bundle-${Date.now()}.zip`;
}

function downloadInBrowser(blob: Blob, filename: string): void {
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = filename;
  link.style.display = "none";
  document.body.appendChild(link);
  link.click();
  link.remove();
  window.setTimeout(() => URL.revokeObjectURL(url), 1_000);
}

export async function saveSupportBundle(): Promise<SupportBundleSaveResult> {
  const response = await fetchWithTimeout(
    apiUrl("/api/runtime/support-bundle"),
    { method: "POST", credentials: "include" },
    120_000,
  );
  if (!response.ok) throw new Error((await response.text()) || response.statusText);
  const filename = supportBundleFilename(response.headers.get("Content-Disposition"));
  const blob = await response.blob();
  if (!isTauriRuntime()) {
    downloadInBrowser(blob, filename);
    return { status: "saved", desktop: false, filename };
  }
  const bytes = new Uint8Array(await blob.arrayBuffer());
  const saved = await invoke<Omit<SavedDesktopSupportBundle, "status" | "desktop"> | null>("save_support_bundle", {
    filename,
    bytes,
  });
  return saved ? { status: "saved", desktop: true, ...saved } : { status: "cancelled" };
}

export async function openSupportBundle(token: string): Promise<void> {
  await invoke("open_meeting_export", { token });
}

export async function revealSupportBundle(token: string): Promise<void> {
  await invoke("reveal_meeting_export", { token });
}

export async function copySupportBundleFile(token: string): Promise<void> {
  await invoke("copy_meeting_export_file", { token });
}
