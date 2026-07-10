import { apiUrl, getAutostartStatus } from "@/lib/backend";
import type { AutostartStatus, MicrophonesResponse, SettingsResponse } from "@/lib/api-types";
import { fetchWithTimeout } from "@/lib/fetch-with-timeout";

interface SettingsBootstrapData {
  settings: SettingsResponse;
  microphones: MicrophonesResponse;
  autostart: AutostartStatus;
}

let cachedBootstrap: { data: SettingsBootstrapData; loadedAt: number } | null = null;
let inflightBootstrap: Promise<SettingsBootstrapData> | null = null;
let bootstrapGeneration = 0;

const SETTINGS_BOOTSTRAP_TTL_MS = 15_000;

export function loadSettingsBootstrap({ force = false }: { force?: boolean } = {}): Promise<SettingsBootstrapData> {
  if (force) {
    bootstrapGeneration += 1;
    cachedBootstrap = null;
  }
  const now = Date.now();
  if (!force && cachedBootstrap && now - cachedBootstrap.loadedAt < SETTINGS_BOOTSTRAP_TTL_MS) {
    return Promise.resolve(cachedBootstrap.data);
  }
  if (!force && inflightBootstrap) {
    return inflightBootstrap;
  }

  const requestGeneration = bootstrapGeneration;
  const request = loadSettingsBootstrapUncached()
    .then((data) => {
      if (requestGeneration === bootstrapGeneration) {
        cachedBootstrap = { data, loadedAt: Date.now() };
      }
      return data;
    })
    .finally(() => {
      if (inflightBootstrap === request) {
        inflightBootstrap = null;
      }
    });
  inflightBootstrap = request;

  return request;
}

export function invalidateSettingsBootstrap() {
  bootstrapGeneration += 1;
  cachedBootstrap = null;
}

async function loadSettingsBootstrapUncached(): Promise<SettingsBootstrapData> {
  const [settingsRes, microphonesRes, autostart] = await Promise.all([
    fetchWithTimeout(apiUrl("/api/settings"), { credentials: "include" }, 10_000),
    fetchWithTimeout(apiUrl("/api/microphones"), { credentials: "include" }, 10_000),
    getAutostartStatus().catch(() => ({ enabled: false, available: false })),
  ]);

  if (!settingsRes.ok) throw new Error(await settingsRes.text());
  if (!microphonesRes.ok) throw new Error(await microphonesRes.text());

  const settings = (await settingsRes.json()) as SettingsResponse;
  const microphones = (await microphonesRes.json()) as MicrophonesResponse;
  return { settings, microphones, autostart };
}
