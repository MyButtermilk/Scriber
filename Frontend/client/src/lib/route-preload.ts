type RoutePreloadPath = "/debug" | "/transcript";

const routeImporters: Record<RoutePreloadPath, () => Promise<unknown>> = {
  "/debug": () => import("@/pages/DebugConsole"),
  "/transcript": () => import("@/pages/TranscriptDetail"),
};

const preloadPromises = new Map<RoutePreloadPath, Promise<unknown>>();

export function preloadRouteChunk(href: string): Promise<unknown> | undefined {
  const path = routePathForHref(href);
  if (!path) return undefined;

  const existing = preloadPromises.get(path);
  if (existing) return existing;

  const promise = routeImporters[path]().catch((error) => {
    preloadPromises.delete(path);
    throw error;
  });
  preloadPromises.set(path, promise);
  return promise;
}

function routePathForHref(href: string): RoutePreloadPath | null {
  if (href === "/debug") return "/debug";
  if (href.startsWith("/transcript/")) return "/transcript";
  return null;
}
