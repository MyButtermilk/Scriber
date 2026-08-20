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

export function targetsSameBackend(candidate: URL, backend: URL): boolean {
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
