// @vitest-environment jsdom
import { act, fireEvent, render, waitFor } from "@testing-library/react";
import { expect, it, vi } from "vitest";
import type { TrayStatus } from "@/lib/backend";

const mocks = vi.hoisted(() => ({
  install: vi.fn(),
  listener: undefined as ((event: { payload: TrayStatus }) => void) | undefined,
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
  hideTrayPanel: async () => undefined,
  trayAction: async () => undefined,
  apiUrl: (path: string) => path,
}));
vi.mock("@tauri-apps/api/app", () => ({ getVersion: async () => "0.5.0" }));
vi.mock("@tauri-apps/api/event", () => ({
  listen: async (_event: string, listener: typeof mocks.listener) => {
    mocks.listener = listener;
    return () => undefined;
  },
}));
vi.mock("@/lib/desktop-updates", () => ({ installDesktopUpdate: mocks.install, checkDesktopUpdate: vi.fn() }));
vi.mock("@/components/ui/wave-physics-loader", () => ({ WavePhysicsLoader: () => <span /> }));
vi.mock("@/i18n", () => {
  const value = { t: (text: string) => text, formatNumber: (number: number) => String(number) };
  return { useI18n: () => value };
});

it("allows a tray retry when another WebView owned the failed update", async () => {
  mocks.install.mockResolvedValue({ phase: "installing" });
  const { default: TrayPanel } = await import("./TrayPanel");
  const view = render(<TrayPanel />);
  try {
    const first = await view.findByRole("button", { name: /Install update/ });
    fireEvent.click(first);
    await waitFor(() => expect(mocks.install).toHaveBeenCalledTimes(1));
    await act(async () => {
      mocks.listener?.({ payload: { ...mocks.ready, recordingMode: "idle", updateInstalling: true } });
    });
    await act(async () => {
      mocks.listener?.({ payload: { ...mocks.ready, recordingMode: "idle", updateInstalling: false } });
    });
    const retry = await view.findByRole("button", { name: /Install update/ });
    expect(retry.hasAttribute("disabled")).toBe(false);
    fireEvent.click(retry);
    await waitFor(() => expect(mocks.install).toHaveBeenCalledTimes(2));
  } finally {
    view.unmount();
  }
});
