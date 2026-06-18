import { apiUrl, getAutostartStatus } from "@/lib/backend";
import type { AutostartStatus, MicrophonesResponse, SettingsResponse } from "@/lib/api-types";

interface SettingsBootstrapData {
  settings: SettingsResponse;
  microphones: MicrophonesResponse;
  autostart: AutostartStatus;
}

let cachedBootstrap: { data: SettingsBootstrapData; loadedAt: number } | null = null;
let inflightBootstrap: Promise<SettingsBootstrapData> | null = null;

const SETTINGS_BOOTSTRAP_TTL_MS = 15_000;

export function loadSettingsBootstrap({ force = false }: { force?: boolean } = {}): Promise<SettingsBootstrapData> {
  const now = Date.now();
  if (!force && cachedBootstrap && now - cachedBootstrap.loadedAt < SETTINGS_BOOTSTRAP_TTL_MS) {
    return Promise.resolve(cachedBootstrap.data);
  }
  if (!force && inflightBootstrap) {
    return inflightBootstrap;
  }

  inflightBootstrap = loadSettingsBootstrapUncached()
    .then((data) => {
      cachedBootstrap = { data, loadedAt: Date.now() };
      return data;
    })
    .finally(() => {
      inflightBootstrap = null;
    });

  return inflightBootstrap;
}

export function invalidateSettingsBootstrap() {
  cachedBootstrap = null;
}

async function loadSettingsBootstrapUncached(): Promise<SettingsBootstrapData> {
  const [settingsRes, microphonesRes, autostart] = await Promise.all([
    fetch(apiUrl("/api/settings"), { credentials: "include" }),
    fetch(apiUrl("/api/microphones"), { credentials: "include" }),
    getAutostartStatus().catch(() => ({ enabled: false, available: false })),
  ]);

  if (!settingsRes.ok) throw new Error(await settingsRes.text());
  if (!microphonesRes.ok) throw new Error(await microphonesRes.text());

  const settings = (await settingsRes.json()) as SettingsResponse;
  const microphones = (await microphonesRes.json()) as MicrophonesResponse;
  return { settings, microphones, autostart };
}
