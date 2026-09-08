// @vitest-environment jsdom
import { beforeEach, describe, expect, it, vi } from "vitest";

const mocks = vi.hoisted(() => ({
  check: vi.fn(),
  invoke: vi.fn(),
  download: vi.fn(),
  close: vi.fn(),
  relaunch: vi.fn(),
  tray: vi.fn(),
}));

vi.mock("@/lib/backend", () => ({ isTauriRuntime: () => true, setTrayUpdateStatus: mocks.tray }));
vi.mock("@tauri-apps/api/app", () => ({ getVersion: async () => "0.5.0" }));
vi.mock("@tauri-apps/api/core", () => ({ invoke: mocks.invoke }));
vi.mock("@tauri-apps/plugin-updater", () => ({ check: mocks.check }));
vi.mock("@tauri-apps/plugin-process", () => ({ relaunch: mocks.relaunch }));

beforeEach(() => {
  vi.resetModules();
  window.localStorage.clear();
  mocks.check.mockReset().mockResolvedValue({
    version: "0.6.0",
    currentVersion: "0.5.0",
    body: "A release",
    downloadAndInstall: mocks.download,
    close: mocks.close,
  });
  mocks.invoke.mockReset().mockResolvedValue(true);
  mocks.download.mockReset().mockResolvedValue(undefined);
  mocks.close.mockReset().mockResolvedValue(undefined);
  mocks.relaunch.mockReset().mockResolvedValue(undefined);
  mocks.tray.mockReset().mockResolvedValue(undefined);
});

describe("shared main-window and tray update installation", () => {
  it("admits one native installation before downloading and reports progress", async () => {
    const order: string[] = [];
    mocks.invoke.mockImplementation(async (command: string) => {
      order.push(command);
      return true;
    });
    mocks.download.mockImplementation(async (progress) => {
      order.push("download");
      progress({ event: "Started", data: { contentLength: 100 } });
      progress({ event: "Progress", data: { chunkLength: 100 } });
      progress({ event: "Finished" });
    });
    const { installDesktopUpdate } = await import("./desktop-updates");
    const progress = vi.fn();
    await installDesktopUpdate(progress);
    expect(order).toEqual(["begin_desktop_update_install", "download", "finish_desktop_update_install"]);
    expect(progress).toHaveBeenCalledWith(expect.objectContaining({ percent: 100 }));
    expect(mocks.relaunch).toHaveBeenCalledOnce();
    expect(mocks.close).toHaveBeenCalledOnce();
    expect(mocks.tray).toHaveBeenCalledWith(expect.objectContaining({ installing: true }));
  });

  it("does not install or release another WebView's admitted update", async () => {
    mocks.invoke.mockResolvedValue(false);
    const { installDesktopUpdate } = await import("./desktop-updates");
    expect((await installDesktopUpdate()).phase).toBe("installing");
    expect(mocks.download).not.toHaveBeenCalled();
    expect(mocks.relaunch).not.toHaveBeenCalled();
    expect(mocks.invoke).toHaveBeenCalledTimes(1);
    expect(mocks.close).toHaveBeenCalledOnce();
  });

  it("releases admission on download failure so the user can retry", async () => {
    mocks.download.mockRejectedValueOnce(new Error("download failed"));
    const { installDesktopUpdate } = await import("./desktop-updates");
    await expect(installDesktopUpdate()).rejects.toThrow("download failed");
    expect(mocks.invoke).toHaveBeenCalledWith("finish_desktop_update_install");
    expect(mocks.close).toHaveBeenCalledOnce();
    await installDesktopUpdate();
    expect(mocks.download).toHaveBeenCalledTimes(2);
  });

  it("coalesces repeated clicks inside the same WebView", async () => {
    const { installDesktopUpdate } = await import("./desktop-updates");
    const first = installDesktopUpdate();
    const second = installDesktopUpdate();
    expect(second).toBe(first);
    await first;
    expect(mocks.download).toHaveBeenCalledOnce();
  });

  it("preserves the original failure when cleanup also fails", async () => {
    mocks.download.mockRejectedValueOnce(new Error("download failed"));
    mocks.invoke.mockImplementation(async (command: string) => {
      if (command === "finish_desktop_update_install") throw new Error("closing shell");
      return true;
    });
    mocks.close.mockRejectedValueOnce(new Error("closing update"));
    const { installDesktopUpdate } = await import("./desktop-updates");
    await expect(installDesktopUpdate()).rejects.toThrow("download failed");
    expect(mocks.close).toHaveBeenCalledOnce();
  });

  it("closes the update resource if native admission cannot be obtained", async () => {
    mocks.invoke.mockRejectedValueOnce(new Error("shell unavailable"));
    const { installDesktopUpdate } = await import("./desktop-updates");
    await expect(installDesktopUpdate()).rejects.toThrow("shell unavailable");
    expect(mocks.download).not.toHaveBeenCalled();
    expect(mocks.close).toHaveBeenCalledOnce();
  });
});
