import { createContext, useContext, useEffect, useState, useCallback, ReactNode } from "react";
import {
    apiUrl,
    backendSessionToken,
    isTauriRuntime,
    loadBackendBaseUrlFromTauri,
    reportFrontendReady,
    setBackendBaseUrl,
    setBackendSessionTokenRequired,
} from "@/lib/backend";
import { REST_API_VERSION, type BackendHealthResponse } from "@/lib/api-types";

interface BackendStatus {
    isOnline: boolean;
    isChecking: boolean;
    hasConnected: boolean;
    checkCount: number;
    lastChecked: Date | null;
    error: string | null;
    checkNow: () => Promise<boolean>;
}

interface TauriBackendStatus {
    baseUrl: string;
    running: boolean;
    ready: boolean;
    managed: boolean;
    pid: number | null;
    message: string;
    runtimeMode: string;
    launchKind: string;
}

const BackendStatusContext = createContext<BackendStatus | null>(null);

const CHECK_INTERVAL_MS = 5000; // Check every 5 seconds when offline
const ONLINE_CHECK_INTERVAL_MS = 30000; // Check every 30 seconds when online

export function BackendStatusProvider({ children }: { children: ReactNode }) {
    const [isOnline, setIsOnline] = useState(true); // Assume online initially
    const [isChecking, setIsChecking] = useState(false);
    const [hasConnected, setHasConnected] = useState(false);
    const [checkCount, setCheckCount] = useState(0);
    const [lastChecked, setLastChecked] = useState<Date | null>(null);
    const [error, setError] = useState<string | null>(null);

    const checkHealth = useCallback(async (): Promise<boolean> => {
        setIsChecking(true);
        try {
            if (isTauriRuntime()) {
                await loadBackendBaseUrlFromTauri();
                try {
                    const { invoke } = await import("@tauri-apps/api/core");
                    const status = await invoke<TauriBackendStatus>("ensure_backend_running");
                    if (status.baseUrl) {
                        setBackendBaseUrl(status.baseUrl);
                    }
                    if (!status.ready) {
                        setIsOnline(false);
                        setError(status.message || (status.running ? "Backend is starting" : "Backend is not running"));
                        setLastChecked(new Date());
                        return false;
                    }

                    setIsOnline(true);
                    setHasConnected(true);
                    setError(null);
                    setLastChecked(new Date());
                    void reportFrontendReady().catch((readyError) => {
                        console.debug("Frontend readiness beacon failed.", readyError);
                    });
                    return true;
                } catch (tauriError) {
                    console.debug("Tauri backend supervisor check failed; falling back to HTTP health probe.", tauriError);
                }
            }

            const controller = new AbortController();
            const timeoutId = setTimeout(() => controller.abort(), 3000);

            const res = await fetch(apiUrl("/api/health"), {
                signal: controller.signal,
            });
            clearTimeout(timeoutId);

            const health = res.ok ? ((await res.json()) as BackendHealthResponse) : null;
            const online = health?.apiVersion === REST_API_VERSION && health.ok === true && health.ready === true;
            if (online) {
                if (!isTauriRuntime() && !backendSessionToken) {
                    const authController = new AbortController();
                    const authTimeoutId = setTimeout(() => authController.abort(), 1500);
                    try {
                        const authProbe = await fetch(apiUrl("/api/runtime"), {
                            signal: authController.signal,
                            cache: "no-store",
                        });
                        if (authProbe.status === 401) {
                            setBackendSessionTokenRequired(true);
                            setIsOnline(false);
                            setError("This backend requires a Scriber desktop session token. Open Scriber from the installed desktop app.");
                            setLastChecked(new Date());
                            return false;
                        }
                        setBackendSessionTokenRequired(false);
                    } finally {
                        clearTimeout(authTimeoutId);
                    }
                } else {
                    setBackendSessionTokenRequired(false);
                }
                try {
                    await reportFrontendReady();
                } catch (readyError) {
                    console.debug("Frontend readiness beacon failed.", readyError);
                }
            }
            setIsOnline(online);
            if (online) {
                setHasConnected(true);
            }
            setError(online ? null : `Server returned ${res.status}`);
            setLastChecked(new Date());
            return online;
        } catch (err) {
            setIsOnline(false);
            if (err instanceof Error) {
                if (err.name === "AbortError") {
                    setError("Connection timed out");
                } else if (err.message.includes("Failed to fetch") || err.message.includes("NetworkError")) {
                    setError("Backend is not running");
                } else {
                    setError(err.message);
                }
            } else {
                setError("Connection failed");
            }
            setLastChecked(new Date());
            return false;
        } finally {
            setCheckCount((count) => count + 1);
            setIsChecking(false);
        }
    }, []);

    // Initial check on mount
    useEffect(() => {
        checkHealth();
    }, [checkHealth]);

    // Periodic health checks
    useEffect(() => {
        const interval = setInterval(
            () => {
                checkHealth();
            },
            isOnline ? ONLINE_CHECK_INTERVAL_MS : CHECK_INTERVAL_MS
        );
        return () => clearInterval(interval);
    }, [isOnline, checkHealth]);

    // Also check when window regains focus
    useEffect(() => {
        const handleFocus = () => {
            checkHealth();
        };
        window.addEventListener("focus", handleFocus);
        return () => window.removeEventListener("focus", handleFocus);
    }, [checkHealth]);

    return (
        <BackendStatusContext.Provider
            value={{
                isOnline,
                isChecking,
                hasConnected,
                checkCount,
                lastChecked,
                error,
                checkNow: checkHealth,
            }}
        >
            {children}
        </BackendStatusContext.Provider>
    );
}

export function useBackendStatus(): BackendStatus {
    const context = useContext(BackendStatusContext);
    if (!context) {
        throw new Error("useBackendStatus must be used within a BackendStatusProvider");
    }
    return context;
}
