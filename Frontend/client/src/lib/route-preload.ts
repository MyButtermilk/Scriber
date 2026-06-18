type RoutePreloadPath = "/youtube" | "/file" | "/settings" | "/debug" | "/transcript";

const routeImporters: Record<RoutePreloadPath, () => Promise<unknown>> = {
  "/youtube": () => import("@/pages/Youtube"),
  "/file": () => import("@/pages/FileTranscribe"),
  "/settings": () => import("@/pages/Settings"),
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

export function preloadPrimaryTabChunks(): () => void {
  if (typeof window === "undefined") {
    return () => {};
  }

  let cancelled = false;
  const cancelIdle = scheduleIdle(() => {
    void preloadRoutesSerially(["/youtube", "/file", "/settings"], () => cancelled);
  });

  return () => {
    cancelled = true;
    cancelIdle();
  };
}

async function preloadRoutesSerially(routes: RoutePreloadPath[], isCancelled: () => boolean) {
  for (const route of routes) {
    if (isCancelled()) return;
    try {
      await preloadRouteChunk(route);
    } catch {
      // Preload is best-effort; real navigation will surface any import failure.
    }
    await nextFrame();
  }
}

function routePathForHref(href: string): RoutePreloadPath | null {
  if (href === "/youtube") return "/youtube";
  if (href === "/file") return "/file";
  if (href === "/settings") return "/settings";
  if (href === "/debug") return "/debug";
  if (href.startsWith("/transcript/")) return "/transcript";
  return null;
}

function scheduleIdle(callback: () => void): () => void {
  if ("requestIdleCallback" in window && typeof window.requestIdleCallback === "function") {
    const handle = window.requestIdleCallback(callback, { timeout: 2500 });
    return () => window.cancelIdleCallback(handle);
  }

  const handle = window.setTimeout(callback, 350);
  return () => window.clearTimeout(handle);
}

function nextFrame(): Promise<void> {
  return new Promise((resolve) => {
    window.requestAnimationFrame(() => resolve());
  });
}
