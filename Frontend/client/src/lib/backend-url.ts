type BackendTransport = "secure" | "insecure";

function backendTransport(protocol: string): BackendTransport | null {
  if (protocol === "https:" || protocol === "wss:") {
    return "secure";
  }
  if (protocol === "http:" || protocol === "ws:") {
    return "insecure";
  }
  return null;
}

function effectivePort(url: URL, transport: BackendTransport): string {
  return url.port || (transport === "secure" ? "443" : "80");
}

function targetsSameBackend(candidate: URL, backend: URL): boolean {
  const candidateTransport = backendTransport(candidate.protocol);
  const configuredTransport = backendTransport(backend.protocol);
  if (candidateTransport === null || configuredTransport === null) {
    return false;
  }
  return (
    candidateTransport === configuredTransport &&
    candidate.hostname === backend.hostname &&
    effectivePort(candidate, candidateTransport) === effectivePort(backend, configuredTransport)
  );
}

export function appendBackendSessionToken(
  url: string,
  backendBaseUrl: string,
  sessionToken: string,
  fallbackOrigin: string,
): string {
  if (!sessionToken) {
    return url;
  }

  try {
    const parsed = new URL(url, backendBaseUrl || fallbackOrigin);
    const backend = new URL(backendBaseUrl || fallbackOrigin);
    const authenticatedPath = parsed.pathname === "/ws" || parsed.pathname.startsWith("/api/");
    if (authenticatedPath && targetsSameBackend(parsed, backend)) {
      parsed.searchParams.set("scriberToken", sessionToken);
      return parsed.toString();
    }
  } catch {
    // Preserve the caller's original URL when parsing fails.
  }
  return url;
}
