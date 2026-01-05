import { createContext, useContext, useEffect, useState, useCallback, ReactNode } from "react";
import { apiUrl } from "@/lib/backend";

interface BackendStatus {
    isOnline: boolean;
    isChecking: boolean;
    lastChecked: Date | null;
    error: string | null;
    checkNow: () => Promise<boolean>;
}

const BackendStatusContext = createContext<BackendStatus | null>(null);

const CHECK_INTERVAL_MS = 5000; // Check every 5 seconds when offline
const ONLINE_CHECK_INTERVAL_MS = 30000; // Check every 30 seconds when online

export function BackendStatusProvider({ children }: { children: ReactNode }) {
    const [isOnline, setIsOnline] = useState(true); // Assume online initially
    const [isChecking, setIsChecking] = useState(false);
    const [lastChecked, setLastChecked] = useState<Date | null>(null);
    const [error, setError] = useState<string | null>(null);

    const checkHealth = useCallback(async (): Promise<boolean> => {
        setIsChecking(true);
        try {
            const controller = new AbortController();
            const timeoutId = setTimeout(() => controller.abort(), 3000);

            const res = await fetch(apiUrl("/api/health"), {
                signal: controller.signal,
            });
            clearTimeout(timeoutId);

            const online = res.ok;
            setIsOnline(online);
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
