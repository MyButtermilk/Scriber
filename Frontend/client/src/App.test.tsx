import type { ReactNode } from "react";
import { act, cleanup, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import App from "./App";

const observed = vi.hoisted(() => ({
  pageRenders: 0,
  transportRenders: 0,
  checkNow: undefined as (() => Promise<boolean>) | undefined,
  fetch: vi.fn(),
  toast: vi.fn(),
  dismiss: vi.fn(),
  translate: (value: string) => value,
}));

vi.mock("@/lib/backend", () => ({
  apiUrl: (path: string) => `http://127.0.0.1:8765${path}`,
  backendSessionToken: "test-token",
  isTauriRuntime: () => false,
  loadBackendBaseUrlFromTauri: async () => undefined,
  reportFrontendReady: async () => undefined,
  setBackendBaseUrl: vi.fn(),
  setBackendSessionTokenRequired: vi.fn(),
  setTrayRecordingState: vi.fn(),
}));
vi.mock("./lib/queryClient", async () => {
  const { QueryClient } = await import("@tanstack/react-query");
  return { queryClient: new QueryClient() };
});
vi.mock("@/lib/tab-data-preload", () => ({ preloadPrimaryTabData: () => undefined }));
vi.mock("@/lib/frontend-performance", () => ({
  flushFrontendPerformanceReport: vi.fn(),
  setFrontendPerformanceReportingEnabled: vi.fn(),
}));
vi.mock("@/hooks/use-device-change-refresh", () => ({ useDeviceChangeRefresh: vi.fn() }));
vi.mock("@/hooks/use-toast", () => ({ useToast: () => ({ toast: observed.toast, dismiss: observed.dismiss }) }));
vi.mock("@/i18n", () => ({ useI18n: () => ({ t: observed.translate }) }));
vi.mock("@/components/theme-provider", () => ({ ThemeProvider: ({ children }: { children: ReactNode }) => children }));
vi.mock("@/components/layout/AppLayout", () => ({ AppLayout: ({ children }: { children: ReactNode }) => children }));
vi.mock("@/components/AppErrorBoundary", () => ({
  AppErrorBoundary: ({ children }: { children: ReactNode }) => children,
}));
vi.mock("@/components/RouteDocumentTitle", () => ({ RouteDocumentTitle: () => null }));
vi.mock("@/components/ui/toaster", () => ({ Toaster: () => null }));
vi.mock("@/components/ui/toast", () => ({ ToastAction: () => null }));
vi.mock("@/components/ui/wave-physics-loader", () => ({ WavePhysicsLoader: () => null }));
vi.mock("@/contexts/WebSocketContext", () => ({
  useSharedWebSocket: () => ({ isConnected: false }),
  WebSocketProvider: ({ children, enabled }: { children: ReactNode; enabled: boolean }) => {
    observed.transportRenders += 1;
    return (
      <div data-testid="transport" data-enabled={String(enabled)}>
        {children}
      </div>
    );
  },
}));
vi.mock("@/components/BackendOfflineBanner", async () => {
  const { useBackendStatus } = await import("@/hooks/use-backend-status");
  return {
    BackendOfflineBanner: function StatusObserver() {
      const status = useBackendStatus();
      return (
        <output data-testid="status">
          {status.isChecking ? "checking" : `${status.isOnline}:${status.checkCount}`}
        </output>
      );
    },
  };
});
vi.mock("@/pages/LiveMic", async () => {
  const { useBackendActions } = await import("@/hooks/use-backend-status");
  return {
    default: function ActionOnlyPage() {
      observed.checkNow = useBackendActions().checkNow;
      observed.pageRenders += 1;
      return <div>Live Mic route</div>;
    },
  };
});
vi.mock("@/pages/Youtube", () => ({ default: () => null }));
vi.mock("@/pages/FileTranscribe", () => ({ default: () => null }));
vi.mock("@/pages/Meetings", () => ({ default: () => null }));
vi.mock("@/pages/Settings", () => ({ default: () => null }));
vi.mock("@/pages/Podcasts", () => ({ default: () => null }));

function health(ready: boolean) {
  return new Response(JSON.stringify({ apiVersion: "1", ok: true, ready }));
}

async function settle() {
  await act(async () => {
    await vi.dynamicImportSettled();
  });
}

beforeEach(() => {
  vi.useFakeTimers();
  window.history.replaceState(null, "", "/");
  observed.pageRenders = 0;
  observed.transportRenders = 0;
  observed.checkNow = undefined;
  observed.fetch.mockReset().mockImplementation(async () => health(true));
  vi.stubGlobal("fetch", observed.fetch);
});

afterEach(() => {
  cleanup();
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

it("keeps the real RuntimeShell route tree stable during checks but updates connectivity transitions", async () => {
  render(<App />);
  await settle();
  expect(screen.getByText("Live Mic route")).toBeInTheDocument();
  expect(screen.getByTestId("status")).toHaveTextContent("true:1");
  const pageRenders = observed.pageRenders;
  const transportRenders = observed.transportRenders;
  let resolveHealth!: (value: Response) => void;
  observed.fetch.mockReturnValueOnce(
    new Promise<Response>((resolve) => {
      resolveHealth = resolve;
    }),
  );

  act(() => {
    void observed.checkNow?.();
  });
  await settle();
  expect(screen.getByTestId("status")).toHaveTextContent("checking");
  expect(observed.pageRenders).toBe(pageRenders);
  expect(observed.transportRenders).toBe(transportRenders);
  await act(async () => resolveHealth(health(true)));
  await settle();
  expect(screen.getByTestId("status")).toHaveTextContent("true:2");
  expect(observed.pageRenders).toBe(pageRenders);
  expect(observed.transportRenders).toBe(transportRenders);

  await act(async () => vi.advanceTimersByTimeAsync(30000));
  expect(screen.getByTestId("status")).toHaveTextContent("true:3");
  expect(observed.pageRenders).toBe(pageRenders);
  expect(observed.transportRenders).toBe(transportRenders);

  observed.fetch.mockResolvedValueOnce(health(false));
  await act(async () => {
    await observed.checkNow?.();
  });
  expect(screen.getByTestId("status")).toHaveTextContent("false:4");
  expect(screen.getByTestId("transport")).toHaveAttribute("data-enabled", "false");
  expect(observed.pageRenders).toBe(pageRenders + 1);
  expect(observed.transportRenders).toBe(transportRenders + 1);

  await act(async () => {
    await observed.checkNow?.();
  });
  expect(screen.getByTestId("transport")).toHaveAttribute("data-enabled", "true");
  expect(observed.pageRenders).toBe(pageRenders + 2);
  expect(observed.transportRenders).toBe(transportRenders + 2);
});
