import { createContext, useContext, useEffect, useState, useCallback, useRef, ReactNode } from "react";
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
    backendStarting: boolean;
    backendMessage: string | null;
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
const TAURI_ACCESS_TIMEOUT_MS = 3000;
const TAURI_SUPERVISOR_TIMEOUT_MS = 5000;

function withDeadline<T>(promise: Promise<T>, timeoutMs: number, label: string): Promise<T> {
    return new Promise<T>((resolve, reject) => {
        const timeoutId = window.setTimeout(
            () => reject(new Error(`${label} timed out`)),
            timeoutMs,
        );
        promise.then(
            (value) => {
                window.clearTimeout(timeoutId);
                resolve(value);
            },
            (error) => {
                window.clearTimeout(timeoutId);
                reject(error);
            },
        );
    });
}

function isManagedBackendStarting(status: TauriBackendStatus): boolean {
    if (!status.managed || !status.running || status.ready) {
        return false;
    }

    const message = status.message.toLowerCase();
    return (
        message.includes("starting") ||
        message.includes("process started") ||
        message.includes("restarting")
    );
}

export function BackendStatusProvider({ children }: { children: ReactNode }) {
    const [isOnline, setIsOnline] = useState(true); // Assume online initially
    const [isChecking, setIsChecking] = useState(false);
    const [hasConnected, setHasConnected] = useState(false);
    const [backendStarting, setBackendStarting] = useState(false);
    const [backendMessage, setBackendMessage] = useState<string | null>(null);
    const [checkCount, setCheckCount] = useState(0);
    const [lastChecked, setLastChecked] = useState<Date | null>(null);
    const [error, setError] = useState<string | null>(null);
    const checkInFlightRef = useRef<Promise<boolean> | null>(null);

    const runHealthCheck = useCallback(async (): Promise<boolean> => {
        setIsChecking(true);
        try {
            if (isTauriRuntime()) {
                try {
                    await withDeadline(
                        loadBackendBaseUrlFromTauri(),
                        TAURI_ACCESS_TIMEOUT_MS,
                        "Tauri backend access lookup",
                    );
                } catch (accessError) {
                    console.debug("Tauri backend access lookup failed; continuing with the known URL.", accessError);
                }
                try {
                    const { invoke } = await import("@tauri-apps/api/core");
                    const status = await withDeadline(
                        invoke<TauriBackendStatus>("ensure_backend_running"),
                        TAURI_SUPERVISOR_TIMEOUT_MS,
                        "Tauri backend supervisor check",
                    );
                    if (status.baseUrl) {
                        setBackendBaseUrl(status.baseUrl);
                    }
                    setBackendMessage(status.message || null);
                    if (!status.ready) {
                        setBackendStarting(isManagedBackendStarting(status));
                        setIsOnline(false);
                        setError(status.message || (status.running ? "Backend is starting" : "Backend is not running"));
                        setLastChecked(new Date());
                        return false;
                    }

                    setBackendStarting(false);
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
            let res: Response;
            try {
                res = await fetch(apiUrl("/api/health"), {
                    signal: controller.signal,
                });
            } finally {
                clearTimeout(timeoutId);
            }

            const health = res.ok ? ((await res.json()) as BackendHealthResponse) : null;
            const online = health?.apiVersion === REST_API_VERSION && health.ok === true && health.ready === true;
            setBackendStarting(false);
            setBackendMessage(null);
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
            setBackendStarting(false);
            setBackendMessage(null);
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

    const checkHealth = useCallback((): Promise<boolean> => {
        const existing = checkInFlightRef.current;
        if (existing) {
            return existing;
        }
        const request = runHealthCheck();
        checkInFlightRef.current = request;
        void request.finally(() => {
            if (checkInFlightRef.current === request) {
                checkInFlightRef.current = null;
            }
        });
        return request;
    }, [runHealthCheck]);

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
                backendStarting,
                backendMessage,
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
