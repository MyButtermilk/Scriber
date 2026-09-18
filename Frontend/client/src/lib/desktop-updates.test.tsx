// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

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

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("startup after the previous process installed or abandoned an update", () => {
  it.each([
    { target: "0.5.115", phase: "current", available: false },
    { target: "0.5.119", phase: "available", available: true },
  ])("does not restore an install admission from cached target $target", async ({ target, phase, available }) => {
    vi.stubGlobal("__SCRIBER_APP_VERSION__", "0.5.115");
    window.localStorage.setItem(
      "scriber:desktop-update-cache:v2",
      JSON.stringify({
        phase: "installing",
        enabled: true,
        currentVersion: "0.5.114",
        version: target,
        lastCheckedAt: new Date().toISOString(),
        message: "Installing update",
      }),
    );

    // Tauri keeps localStorage across process restarts; its atomic install gate
    // starts clear. Independent WebViews load separate JS module instances.
    const mainWindow = await import("./desktop-updates");
    vi.resetModules();
    const trayWindow = await import("./desktop-updates");
    expect(mainWindow.installDesktopUpdate).not.toBe(trayWindow.installDesktopUpdate);

    const initial = mainWindow.getCachedDesktopUpdateStatus();
    mainWindow.publishDesktopUpdateStatusToTray(initial);
    expect(initial).toMatchObject({ phase, available, currentVersion: "0.5.115" });
    expect(initial.message).not.toMatch(/installing|restarting/i);
    expect(initial.lastCheckedAt).toBeUndefined();
    expect(mocks.tray).toHaveBeenCalledWith(expect.objectContaining({ installing: false, available }));
    expect(JSON.parse(window.localStorage.getItem("scriber:desktop-update-cache:v2")!)).toMatchObject({ phase });
    const [mainCheck, trayCheck] = await Promise.all([
      mainWindow.checkDesktopUpdateIfDue(),
      trayWindow.checkDesktopUpdateIfDue(),
    ]);
    expect(mocks.invoke).not.toHaveBeenCalled();
    expect(mocks.check).toHaveBeenCalledTimes(2);
    expect(mainCheck.reason).toBe("checked");
    expect(trayCheck.reason).toBe("checked");
    expect(mainCheck.status.phase).not.toBe("installing");
    expect(trayCheck.status.phase).not.toBe("installing");
  });
});

describe("shared main-window and tray update installation", () => {
  it("keeps a real in-process admission exclusive across independent WebView modules", async () => {
    let active = false;
    let nativeInstalling = false;
    let rejectDownload!: (reason: Error) => void;
    const downloading = new Promise<void>((_resolve, reject) => {
      rejectDownload = reject;
    });
    mocks.invoke.mockImplementation(async (command: string) => {
      if (command === "begin_desktop_update_install") {
        if (active) return false;
        active = true;
        nativeInstalling = true;
        return true;
      }
      if (command === "finish_desktop_update_install") {
        active = false;
        nativeInstalling = false;
      }
      return undefined;
    });
    mocks.tray.mockImplementation(async (status: { installing: boolean }) => {
      nativeInstalling = status.installing || active;
    });
    mocks.download.mockImplementation(async (progress) => {
      progress({ event: "Started", data: { contentLength: 100 } });
      progress({ event: "Progress", data: { chunkLength: 50 } });
      await downloading;
    });
    const mainWindow = await import("./desktop-updates");
    vi.resetModules();
    const trayWindow = await import("./desktop-updates");
    const mainProgress = vi.fn();
    const trayProgress = vi.fn();
    const results = Promise.allSettled([
      mainWindow.installDesktopUpdate(mainProgress),
      trayWindow.installDesktopUpdate(trayProgress),
    ]);

    await vi.waitFor(() => expect(mocks.close).toHaveBeenCalledOnce());
    expect(mocks.invoke.mock.calls.filter(([command]) => command === "begin_desktop_update_install")).toHaveLength(2);
    expect(mocks.download).toHaveBeenCalledOnce();
    expect(active).toBe(true);
    expect(nativeInstalling).toBe(true);
    expect([mainProgress, trayProgress].filter((progress) => progress.mock.calls.length > 0)).toHaveLength(1);
    const ownerWindow = mainProgress.mock.calls.length > 0 ? mainWindow : trayWindow;
    const observingWindow = ownerWindow === mainWindow ? trayWindow : mainWindow;
    expect(ownerWindow.getCachedDesktopUpdateStatus().phase).toBe("installing");
    expect(ownerWindow.getCachedDesktopUpdateStatus().available).toBe(false);
    expect(observingWindow.getCachedDesktopUpdateStatus().phase).toBe("available");
    observingWindow.publishDesktopUpdateStatusToTray(observingWindow.getCachedDesktopUpdateStatus());
    expect(nativeInstalling).toBe(true);
    const persisted = JSON.parse(window.localStorage.getItem("scriber:desktop-update-cache:v2")!);
    expect(persisted).toMatchObject({ phase: "available", version: "0.6.0" });
    expect(persisted.message).not.toMatch(/installing|restarting/i);

    rejectDownload(new Error("network disconnected"));
    const settled = await results;
    expect(settled.filter((result) => result.status === "rejected")).toHaveLength(1);
    expect(mocks.invoke.mock.calls.filter(([command]) => command === "finish_desktop_update_install")).toHaveLength(1);
    expect(active).toBe(false);
    expect(nativeInstalling).toBe(false);
    expect(ownerWindow.getCachedDesktopUpdateStatus().phase).toBe("available");
    expect(mocks.relaunch).not.toHaveBeenCalled();
    expect(mocks.close).toHaveBeenCalledTimes(2);
  });

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
    const { getCachedDesktopUpdateStatus } = await import("./desktop-updates");
    expect(getCachedDesktopUpdateStatus().phase).toBe("available");
    expect(JSON.parse(window.localStorage.getItem("scriber:desktop-update-cache:v2")!).phase).toBe("available");
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
