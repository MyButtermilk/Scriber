import { beforeEach, expect, it, vi } from "vitest";
import { getAutostartStatus } from "./backend";
import { fetchWithTimeout } from "./fetch-with-timeout";
import {
  invalidateSettingsBootstrap,
  loadSettingsBootstrap,
  loadSettingsBootstrapResources,
} from "./settings-bootstrap";

vi.mock("./backend", () => ({ apiUrl: (path: string) => path, getAutostartStatus: vi.fn() }));
vi.mock("./fetch-with-timeout", () => ({ fetchWithTimeout: vi.fn() }));

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (reason: Error) => void;
  const promise = new Promise<T>((yes, no) => {
    resolve = yes;
    reject = no;
  });
  return { promise, resolve, reject };
}

beforeEach(() => {
  invalidateSettingsBootstrap();
  vi.mocked(getAutostartStatus).mockResolvedValue({ enabled: false, available: true });
  vi.mocked(fetchWithTimeout).mockImplementation(async (url) =>
    Response.json(String(url).endsWith("/settings") ? { language: "en" } : { devices: [] }),
  );
});

it("shares idle and page requests but exposes settings before slow peripherals", async () => {
  const microphone = deferred<Response>();
  const autostart = deferred<{ enabled: boolean; available: boolean }>();
  vi.mocked(getAutostartStatus).mockReturnValue(autostart.promise);
  vi.mocked(fetchWithTimeout).mockImplementation(async (url) =>
    String(url).endsWith("/settings") ? Response.json({ language: "de" }) : microphone.promise,
  );
  const preload = loadSettingsBootstrap();
  const resources = loadSettingsBootstrapResources();
  const finished = vi.fn();
  void preload.then(finished);
  await expect(resources.settings).resolves.toEqual({ language: "de" });
  expect(finished).not.toHaveBeenCalled();
  expect(fetchWithTimeout).toHaveBeenCalledTimes(2);
  expect(getAutostartStatus).toHaveBeenCalledTimes(1);
  microphone.resolve(Response.json({ devices: [] }));
  autostart.resolve({ enabled: true, available: true });
  await expect(preload).resolves.toMatchObject({ settings: { language: "de" }, autostart: { enabled: true } });
  await loadSettingsBootstrap();
  expect(fetchWithTimeout).toHaveBeenCalledTimes(2);
});

it("keeps settings and autostart usable if microphone discovery fails, and allows retry", async () => {
  vi.mocked(fetchWithTimeout).mockImplementation(async (url) =>
    String(url).endsWith("/settings")
      ? Response.json({ language: "de" })
      : new Response("device failed", { status: 500 }),
  );
  const resources = loadSettingsBootstrapResources();
  await expect(resources.settings).resolves.toEqual({ language: "de" });
  await expect(resources.autostart).resolves.toEqual({ enabled: false, available: true });
  await expect(resources.microphones).rejects.toThrow("device failed");
  await expect(resources.complete).rejects.toThrow("device failed");
  vi.mocked(fetchWithTimeout).mockImplementation(async () => Response.json({ devices: [] }));
  await loadSettingsBootstrap();
  expect(fetchWithTimeout).toHaveBeenCalledTimes(4);
});

it.each(["invalidate", "force"])(
  "%s prevents an older in-flight snapshot from being reused or cached",
  async (mode) => {
    const oldSettings = deferred<Response>();
    vi.mocked(fetchWithTimeout).mockImplementationOnce(() => oldSettings.promise);
    const old = loadSettingsBootstrap();
    if (mode === "invalidate") invalidateSettingsBootstrap();
    const current = await loadSettingsBootstrap({ force: mode === "force" });
    expect(current.settings).toEqual({ language: "en" });
    oldSettings.resolve(Response.json({ language: "stale" }));
    await old;
    expect((await loadSettingsBootstrap()).settings).toEqual({ language: "en" });
    expect(fetchWithTimeout).toHaveBeenCalledTimes(4);
  },
);

it("expires the complete snapshot and deduplicates the replacement", async () => {
  const now = vi.spyOn(Date, "now").mockReturnValue(0);
  await loadSettingsBootstrap();
  now.mockReturnValue(14_999);
  await loadSettingsBootstrap();
  expect(fetchWithTimeout).toHaveBeenCalledTimes(2);
  now.mockReturnValue(15_000);
  await Promise.all([loadSettingsBootstrap(), loadSettingsBootstrap()]);
  expect(fetchWithTimeout).toHaveBeenCalledTimes(4);
});

it("preserves primary errors and treats unavailable native autostart as optional", async () => {
  vi.mocked(getAutostartStatus).mockRejectedValue(new Error("native unavailable"));
  vi.mocked(fetchWithTimeout).mockImplementation(async (url) =>
    String(url).endsWith("/settings")
      ? new Response("settings failed", { status: 500 })
      : Response.json({ devices: [] }),
  );
  const resources = loadSettingsBootstrapResources();
  await expect(resources.settings).rejects.toThrow("settings failed");
  await expect(resources.complete).rejects.toThrow("settings failed");
  await expect(resources.autostart).resolves.toEqual({ enabled: false, available: false });
});
