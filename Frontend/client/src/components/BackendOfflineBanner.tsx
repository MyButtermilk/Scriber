import { useEffect, useState } from "react";
import { useBackendStatus } from "@/hooks/use-backend-status";
import { AlertCircle, RefreshCw, Loader2 } from "lucide-react";
import { Button } from "@/components/ui/button";

const STARTUP_GRACE_MS = 9000;
const STARTUP_RECOVERABLE_ERRORS = new Set([
    "Backend is starting",
    "Managed backend process started",
    "Managed backend is starting",
    "Backend is not running",
    "Connection timed out",
    "Connection failed",
]);

export function BackendOfflineBanner() {
    const {
        isOnline,
        isChecking,
        hasConnected,
        backendStarting,
        backendMessage,
        error,
        checkNow,
    } = useBackendStatus();
    const [startupGraceElapsed, setStartupGraceElapsed] = useState(false);

    useEffect(() => {
        if (hasConnected) {
            setStartupGraceElapsed(true);
            return;
        }

        setStartupGraceElapsed(false);
        const timeoutId = window.setTimeout(() => {
            setStartupGraceElapsed(true);
        }, STARTUP_GRACE_MS);

        return () => window.clearTimeout(timeoutId);
    }, [hasConnected]);

    if (isOnline) {
        return null;
    }

    const isStartupRecoverable = !error || STARTUP_RECOVERABLE_ERRORS.has(error);
    const showStartup = !hasConnected && (backendStarting || (!startupGraceElapsed && isStartupRecoverable));
    const startupDetail = backendStarting
        ? (backendMessage || "Managed backend is starting")
        : "Connecting to the local API";

    if (showStartup) {
        return (
            <div className="fixed inset-0 z-50 flex items-center justify-center bg-background/98 px-5 backdrop-blur-md">
                <div className="w-full max-w-[31rem] rounded-[1.75rem] border border-border/60 bg-card/90 p-7 text-card-foreground shadow-[0_24px_80px_-42px_rgba(15,23,42,0.55)] ring-1 ring-white/10 sm:p-8">
                    <div className="flex flex-col items-center text-center">
                        <div className="relative mb-6 flex h-20 w-20 items-center justify-center">
                            <div className="absolute inset-0 rounded-full bg-primary/10 scriber-startup-pulse" />
                            <div className="relative flex h-14 w-14 items-center justify-center rounded-full border border-primary/20 bg-card shadow-[inset_0_1px_0_rgba(255,255,255,0.38)]">
                                <img
                                    src="/favicon.svg"
                                    alt=""
                                    aria-hidden="true"
                                    className="h-9 w-9 object-contain"
                                    draggable={false}
                                />
                            </div>
                        </div>

                        <p className="mb-2 text-xs font-semibold uppercase tracking-[0.22em] text-muted-foreground">
                            Local service
                        </p>
                        <h2 className="mb-3 text-2xl font-semibold tracking-tight text-foreground">
                            Starting Scriber
                        </h2>
                        <p className="max-w-sm text-sm leading-6 text-muted-foreground">
                            The desktop backend is coming online. This usually takes a few seconds after launch.
                        </p>

                        <div className="mt-7 w-full max-w-sm">
                            <div className="h-2 overflow-hidden rounded-full bg-muted shadow-[inset_0_1px_3px_rgba(15,23,42,0.18)]">
                                <div className="h-full w-2/5 rounded-full bg-primary/80 scriber-startup-progress" />
                            </div>
                            <div className="mt-4 flex items-center justify-center gap-2 text-xs font-medium text-muted-foreground">
                                <Loader2 className="h-3.5 w-3.5 animate-spin" aria-hidden="true" />
                                {startupDetail}
                            </div>
                        </div>
                    </div>
                </div>
            </div>
        );
    }

    return (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-background/95 px-5 backdrop-blur-sm">
            <div className="w-full max-w-md rounded-[1.5rem] border border-destructive/25 bg-card p-8 shadow-[0_24px_80px_-42px_rgba(15,23,42,0.6)]">
                <div className="flex flex-col items-center text-center">
                    <div className="mb-5 flex h-16 w-16 items-center justify-center rounded-full bg-destructive/10">
                        <AlertCircle className="h-8 w-8 text-destructive" aria-hidden="true" />
                    </div>

                    <h2 className="mb-3 text-xl font-semibold tracking-tight text-foreground">
                        Backend Not Available
                    </h2>

                    <p className="mb-6 leading-6 text-muted-foreground">
                        {error === "Backend is not running" ? (
                            <>
                                Scriber tried to start the local backend, but it is not ready yet.
                                Restart the backend from the tray icon or try again.
                            </>
                        ) : error === "Connection timed out" ? (
                            <>
                                The backend is taking too long to respond. It may be starting up or
                                experiencing issues.
                            </>
                        ) : (
                            <>
                                Unable to connect to the backend service. Please ensure the application
                                is running properly.
                            </>
                        )}
                        {error && !["Backend is not running", "Connection timed out"].includes(error) && (
                            <span className="mt-3 block text-sm">{error}</span>
                        )}
                    </p>

                    <div className="flex flex-col gap-3 sm:flex-row">
                        <Button
                            variant="outline"
                            onClick={checkNow}
                            disabled={isChecking}
                            className="min-w-[140px]"
                        >
                            {isChecking ? (
                                <>
                                    <Loader2 className="mr-2 h-4 w-4 animate-spin" />
                                    Checking...
                                </>
                            ) : (
                                <>
                                    <RefreshCw className="mr-2 h-4 w-4" />
                                    Retry Connection
                                </>
                            )}
                        </Button>
                    </div>

                    <p className="mt-6 text-xs text-muted-foreground/70">
                        The app will automatically reconnect when the backend becomes available.
                    </p>
                </div>
            </div>
        </div>
    );
}
