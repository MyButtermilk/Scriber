const configuredBase = (import.meta.env.VITE_BACKEND_URL as string | undefined)?.trim();
const defaultBase = "http://127.0.0.1:8765";

export const backendBaseUrl = (configuredBase || defaultBase).replace(/\/+$/, "");

export function apiUrl(pathOrUrl: string): string {
  if (!pathOrUrl) return pathOrUrl;
  if (/^https?:\/\//i.test(pathOrUrl)) return pathOrUrl;
  if (!backendBaseUrl) return pathOrUrl;
  return pathOrUrl.startsWith("/")
    ? `${backendBaseUrl}${pathOrUrl}`
    : `${backendBaseUrl}/${pathOrUrl}`;
}

export function wsUrl(path: string): string {
  const base = backendBaseUrl || window.location.origin;
  const wsBase = base.replace(/^http:/i, "ws:").replace(/^https:/i, "wss:");
  return path.startsWith("/") ? `${wsBase}${path}` : `${wsBase}/${path}`;
}
