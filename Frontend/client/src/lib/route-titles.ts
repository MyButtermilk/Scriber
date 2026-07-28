export type RouteTitleSource =
  "Console" | "File" | "Live Mic" | "Meetings" | "Page not found" | "Settings" | "Transcript" | "YouTube";

export function routeTitleSource(location: string): RouteTitleSource {
  const path = location.split(/[?#]/, 1)[0] || "/";

  if (path === "/") return "Live Mic";
  if (path === "/meetings" || path.startsWith("/meetings/")) return "Meetings";
  if (path === "/youtube") return "YouTube";
  if (path === "/file") return "File";
  if (path === "/debug") return "Console";
  if (path === "/settings") return "Settings";
  if (/^\/transcript\/[^/]+\/?$/.test(path)) return "Transcript";
  return "Page not found";
}
