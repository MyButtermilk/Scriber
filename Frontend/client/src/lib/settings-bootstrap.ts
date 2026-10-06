import { apiUrl, getAutostartStatus } from "@/lib/backend";
import type { AutostartStatus, MicrophonesResponse, SettingsResponse } from "@/lib/api-types";
import { fetchWithTimeout } from "@/lib/fetch-with-timeout";

interface SettingsBootstrapData {
  settings: SettingsResponse;
  microphones: MicrophonesResponse;
  autostart: AutostartStatus;
}

interface SettingsBootstrapResources {
  settings: Promise<SettingsResponse>;
  microphones: Promise<MicrophonesResponse>;
  autostart: Promise<AutostartStatus>;
  complete: Promise<SettingsBootstrapData>;
}

let cachedBootstrap: { resources: SettingsBootstrapResources; loadedAt: number } | null = null;
let inflightBootstrap: SettingsBootstrapResources | null = null;
let bootstrapGeneration = 0;

const SETTINGS_BOOTSTRAP_TTL_MS = 15_000;

export function loadSettingsBootstrap(options: { force?: boolean } = {}): Promise<SettingsBootstrapData> {
  return loadSettingsBootstrapResources(options).complete;
}

// Share requests with idle preloading, while allowing the page to show settings
// before device discovery or the native autostart query finishes.
export function loadSettingsBootstrapResources({
  force = false,
}: { force?: boolean } = {}): SettingsBootstrapResources {
  if (force) invalidateSettingsBootstrap();
  if (cachedBootstrap && Date.now() - cachedBootstrap.loadedAt < SETTINGS_BOOTSTRAP_TTL_MS) {
    return cachedBootstrap.resources;
  }
  if (inflightBootstrap) return inflightBootstrap;

  const requestGeneration = bootstrapGeneration;
  const settings = fetchBootstrapJson<SettingsResponse>("/api/settings");
  const microphones = fetchBootstrapJson<MicrophonesResponse>("/api/microphones");
  const autostart = getAutostartStatus().catch(() => ({ enabled: false, available: false }));
  const resources: SettingsBootstrapResources = {
    settings,
    microphones,
    autostart,
    complete: Promise.all([settings, microphones, autostart])
      .then(([settings, microphones, autostart]) => {
        if (requestGeneration === bootstrapGeneration) {
          cachedBootstrap = { resources, loadedAt: Date.now() };
        }
        return { settings, microphones, autostart };
      })
      .finally(() => {
        if (inflightBootstrap === resources) inflightBootstrap = null;
      }),
  };
  // Resource consumers handle failures separately; the aggregate may have no
  // consumer. Observe its rejection without changing the public promise.
  void resources.complete.catch(() => {});
  inflightBootstrap = resources;
  return resources;
}

export function invalidateSettingsBootstrap() {
  bootstrapGeneration += 1;
  cachedBootstrap = null;
  inflightBootstrap = null;
}

async function fetchBootstrapJson<T>(path: string): Promise<T> {
  const response = await fetchWithTimeout(apiUrl(path), { credentials: "include" }, 10_000);
  if (!response.ok) throw new Error(await response.text());
  return response.json() as Promise<T>;
}
