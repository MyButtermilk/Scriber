// @vitest-environment jsdom
import { act, fireEvent, render, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import type { TrayStatus } from "@/lib/backend";

const mocks = vi.hoisted(() => ({
  install: vi.fn(),
  fetch: vi.fn(),
  action: vi.fn(),
  hide: vi.fn(),
  cleanups: [] as string[],
  listeners: new Map<string, (event: { payload: TrayStatus }) => void>(),
  ready: {
    recordingActive: false,
    recordingMode: "idle",
    updateAvailable: true,
    updateInstalling: false,
    updateMessage: "",
  },
}));

vi.mock("@/lib/backend", () => ({
  isTauriRuntime: () => true,
  loadBackendBaseUrlFromTauri: async () => undefined,
  getTrayStatus: async () => mocks.ready,
  getGlobalHotkeyStatus: async () => ({ hotkey: "Ctrl+F9" }),
  refreshGlobalHotkey: async () => ({ hotkey: "Ctrl+F9" }),
  hideTrayPanel: mocks.hide,
  trayAction: mocks.action,
  apiUrl: (path: string) => path,
}));
vi.mock("@tauri-apps/api/app", () => ({ getVersion: async () => "0.5.0" }));
vi.mock("@tauri-apps/api/event", () => ({
  listen: async (event: string, listener: (event: { payload: TrayStatus }) => void) => {
    mocks.listeners.set(event, listener);
    return () => {
      mocks.listeners.delete(event);
      mocks.cleanups.push(event);
    };
  },
}));
vi.mock("@/lib/desktop-updates", () => ({ installDesktopUpdate: mocks.install, checkDesktopUpdate: vi.fn() }));
vi.mock("@/components/ui/wave-physics-loader", () => ({ WavePhysicsLoader: () => <span /> }));
vi.mock("@/lib/fetch-with-timeout", () => ({ fetchWithTimeout: mocks.fetch }));
vi.mock("@/i18n", () => {
  const value = {
    t: (text: string) => text,
    formatNumber: (number: number) => String(number),
    formatDate: (date: string) => date,
    formatLegacyDate: (date: string) => date,
  };
  return { useI18n: () => value };
});

function response(items: unknown[] = []) {
  return { ok: true, json: async () => ({ items }) };
}

function item(id: string, preview: string, contentAvailable = true) {
  return { id, preview, contentAvailable, status: "completed", type: "mic", date: "today", duration: "00:10" };
}

function opened() {
  mocks.listeners.get("scriber-tray-opened")?.({ payload: { ...mocks.ready, recordingMode: "idle" } });
}

beforeEach(() => {
  mocks.listeners.clear();
  mocks.cleanups.length = 0;
  mocks.fetch.mockReset().mockResolvedValue(response());
  mocks.action.mockReset().mockResolvedValue(undefined);
  mocks.hide.mockReset().mockResolvedValue(undefined);
});

afterEach(() => vi.useRealTimers());

it("allows a tray retry when another WebView owned the failed update", async () => {
  mocks.install.mockResolvedValue({ phase: "installing" });
  const { default: TrayPanel } = await import("./TrayPanel");
  const view = render(<TrayPanel />);
  try {
    const first = await view.findByRole("button", { name: /Install update/ });
    fireEvent.click(first);
    await waitFor(() => expect(mocks.install).toHaveBeenCalledTimes(1));
    await act(async () => {
      mocks.listeners.get("scriber-tray-status")?.({
        payload: { ...mocks.ready, recordingMode: "idle", updateInstalling: true },
      });
    });
    await act(async () => {
      mocks.listeners.get("scriber-tray-status")?.({
        payload: { ...mocks.ready, recordingMode: "idle", updateInstalling: false },
      });
    });
    const retry = await view.findByRole("button", { name: /Install update/ });
    expect(retry.hasAttribute("disabled")).toBe(false);
    fireEvent.click(retry);
    await waitFor(() => expect(mocks.install).toHaveBeenCalledTimes(2));
  } finally {
    view.unmount();
  }
});

it("refreshes on every native opening and recent-view entry, keeping visible IDs attached to previews", async () => {
  let items = [item("mic-1", "First dictation")];
  mocks.fetch.mockImplementation(async () => response(items));
  const { default: TrayPanel } = await import("./TrayPanel");
  const view = render(<TrayPanel />);
  try {
    await waitFor(() => expect(Array.from(mocks.listeners.keys())).toContain("scriber-tray-opened"));
    await waitFor(() => expect(mocks.fetch).toHaveBeenCalledTimes(1));
    fireEvent.click(view.getByRole("button", { name: /Recent Transcripts/ }));
    await view.findByRole("button", { name: /First dictation/ });
    items = [item("mic-2", "Current dictation")];
    await act(async () => opened());
    const current = await view.findByRole("button", { name: /Current dictation/ });
    expect(view.queryByRole("button", { name: /First dictation/ })).toBeNull();
    fireEvent.click(current);
    await waitFor(() => expect(mocks.action).toHaveBeenCalledWith("copy_transcript:mic-2"));
    fireEvent.click(view.getByRole("button", { name: "Back to tray menu" }));
    items = [item("mic-3", "Another dictation")];
    fireEvent.click(view.getByRole("button", { name: /Recent Transcripts/ }));
    await view.findByRole("button", { name: /Another dictation/ });
    expect(mocks.fetch).toHaveBeenCalledTimes(4);
  } finally {
    view.unmount();
  }
  expect(mocks.cleanups).toContain("scriber-tray-opened");
});

it("discards older responses and errors after a newer opening even when abort is ignored", async () => {
  const pending: Array<(value: ReturnType<typeof response>) => void> = [];
  mocks.fetch.mockImplementation(() => new Promise((resolve) => pending.push(resolve)));
  const { default: TrayPanel } = await import("./TrayPanel");
  const view = render(<TrayPanel />);
  try {
    await waitFor(() => expect(pending.length).toBe(1));
    fireEvent.click(view.getByRole("button", { name: /Recent Transcripts/ }));
    await waitFor(() => expect(pending.length).toBe(2));
    await act(async () => opened());
    await act(async () => pending[2](response([item("new", "Newest text")])));
    await view.findByRole("button", { name: /Newest text/ });
    await act(async () => pending[1](response([item("old", "Obsolete text")])));
    await act(async () => pending[0]({ ok: false, json: async () => ({ items: [] }) }));
    expect(view.queryByText("Obsolete text")).toBeNull();
    expect(view.queryByText("Could not load recent transcripts.")).toBeNull();
    expect(view.getByRole("button", { name: /Newest text/ })).toBeTruthy();
  } finally {
    view.unmount();
  }
});

it("removes stale choices on refresh failure and allows explicit retry", async () => {
  mocks.fetch.mockResolvedValue(response([item("old", "Previous text")]));
  const { default: TrayPanel } = await import("./TrayPanel");
  const view = render(<TrayPanel />);
  try {
    await waitFor(() => expect(mocks.fetch).toHaveBeenCalledTimes(1));
    fireEvent.click(view.getByRole("button", { name: /Recent Transcripts/ }));
    await view.findByRole("button", { name: /Previous text/ });
    mocks.fetch.mockRejectedValue(new Error("private backend response"));
    await act(async () => opened());
    await view.findByText("Could not load recent transcripts.");
    expect(view.queryByText("Previous text")).toBeNull();
    expect(view.queryByText("private backend response")).toBeNull();
    mocks.fetch.mockResolvedValue(response([item("new", "Retry text")]));
    fireEvent.click(view.getByRole("button", { name: "Refresh recent transcripts" }));
    await view.findByRole("button", { name: /Retry text/ });
  } finally {
    view.unmount();
  }
});

it("renders bounded safe previews, disables empty content, and excludes invalid or unfinished entries", async () => {
  mocks.fetch.mockResolvedValue(
    response([
      item("safe", "  Hello\n\t<b>&World</b>\u202e " + "😀".repeat(100)),
      item("empty", "", false),
      { ...item("unavailable", "", false), contentUnavailable: true },
      { ...item("unfinished", "Unfinished text"), status: "processing" },
      item("../bad", "Unsafe ID"),
      item("safe", "Duplicate"),
    ]),
  );
  const { default: TrayPanel } = await import("./TrayPanel");
  const view = render(<TrayPanel />);
  try {
    await waitFor(() => expect(mocks.fetch).toHaveBeenCalledTimes(1));
    fireEvent.click(view.getByRole("button", { name: /Recent Transcripts/ }));
    const safe = await view.findByRole("button", { name: /Hello <b>&World<\/b>/ });
    const label = safe.querySelector("span.min-w-0 > span")?.textContent || "";
    expect(Array.from(label)).toHaveLength(72);
    expect(safe.querySelector("b")).toBeNull();
    expect(safe.textContent).not.toContain("\u202e");
    expect(view.getByRole("button", { name: /Empty transcript/ }).hasAttribute("disabled")).toBe(true);
    const unavailable = view.getByRole("button", { name: /Transcript unavailable\. Refresh recent transcripts\./ });
    expect(unavailable.hasAttribute("disabled")).toBe(true);
    fireEvent.click(unavailable);
    expect(mocks.action).not.toHaveBeenCalled();
    expect(view.queryByText("Unfinished text")).toBeNull();
    expect(view.queryByText("Unsafe ID")).toBeNull();
    expect(view.queryByText("Duplicate")).toBeNull();
    fireEvent.click(safe);
    await waitFor(() => expect(mocks.action).toHaveBeenCalledWith("copy_transcript:safe"));
  } finally {
    view.unmount();
  }
});

it("serializes copy and does not hide a reopened panel when an older copy finishes", async () => {
  let finishCopy!: () => void;
  mocks.action.mockImplementation(
    () =>
      new Promise<void>((resolve) => {
        finishCopy = resolve;
      }),
  );
  mocks.fetch.mockResolvedValue(response([item("one", "First text"), item("two", "Second text")]));
  const { default: TrayPanel } = await import("./TrayPanel");
  const view = render(<TrayPanel />);
  try {
    await waitFor(() => expect(mocks.fetch).toHaveBeenCalledTimes(1));
    fireEvent.click(view.getByRole("button", { name: /Recent Transcripts/ }));
    const first = await view.findByRole("button", { name: /First text/ });
    fireEvent.click(first);
    fireEvent.click(view.getByRole("button", { name: /Second text/ }));
    expect(mocks.action).toHaveBeenCalledTimes(1);
    await act(async () => opened());
    await view.findByRole("button", { name: /First text/ });
    vi.useFakeTimers();
    await act(async () => finishCopy());
    await act(async () => vi.advanceTimersByTime(700));
    expect(mocks.hide).not.toHaveBeenCalled();
    expect(view.queryByText("Copied to clipboard")).toBeNull();
  } finally {
    view.unmount();
  }
});

it("cancels a pending blur hide on native reopen and cleans listeners on unmount", async () => {
  const { default: TrayPanel } = await import("./TrayPanel");
  const view = render(<TrayPanel />);
  await waitFor(() => expect(mocks.fetch).toHaveBeenCalledTimes(1));
  vi.useFakeTimers();
  fireEvent.blur(window);
  await act(async () => opened());
  await act(async () => vi.advanceTimersByTime(200));
  expect(mocks.hide).not.toHaveBeenCalled();
  view.unmount();
  expect(mocks.listeners.size).toBe(0);
});

it("reports a selected transcript deleted before copying without false clipboard success", async () => {
  mocks.fetch.mockResolvedValue(response([item("deleted", "Deleted since opening")]));
  mocks.action.mockRejectedValue("transcript_unavailable");
  const { default: TrayPanel } = await import("./TrayPanel");
  const view = render(<TrayPanel />);
  try {
    await waitFor(() => expect(mocks.fetch).toHaveBeenCalledTimes(1));
    fireEvent.click(view.getByRole("button", { name: /Recent Transcripts/ }));
    fireEvent.click(await view.findByRole("button", { name: /Deleted since opening/ }));
    await view.findByText("Transcript unavailable. Refresh recent transcripts.");
    expect(view.queryByText("Copied to clipboard")).toBeNull();
    expect(mocks.hide).not.toHaveBeenCalled();
    mocks.action.mockRejectedValue("transcript_unsupported_text");
    fireEvent.click(view.getByRole("button", { name: /Deleted since opening/ }));
    await view.findByText("This transcript contains unsupported characters and cannot be copied.");
    expect(view.queryByText("Copied to clipboard")).toBeNull();
    expect(mocks.hide).not.toHaveBeenCalled();
  } finally {
    view.unmount();
  }
});

it("aborts pending history reads on unmount and does not leave an opened listener behind", async () => {
  let resolve!: (value: ReturnType<typeof response>) => void;
  mocks.fetch.mockImplementation(
    () =>
      new Promise((done) => {
        resolve = done;
      }),
  );
  const { default: TrayPanel } = await import("./TrayPanel");
  const view = render(<TrayPanel />);
  await waitFor(() => expect(mocks.fetch).toHaveBeenCalledTimes(1));
  const signal = mocks.fetch.mock.calls[0][1].signal as AbortSignal;
  view.unmount();
  expect(signal.aborted).toBe(true);
  expect(mocks.listeners.size).toBe(0);
  await act(async () => resolve(response([item("late", "Late answer")])));
  expect(mocks.hide).not.toHaveBeenCalled();
});
