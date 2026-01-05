import { useBackendStatus } from "@/hooks/use-backend-status";
import { AlertCircle, RefreshCw, Loader2 } from "lucide-react";
import { Button } from "@/components/ui/button";

export function BackendOfflineBanner() {
    const { isOnline, isChecking, error, checkNow } = useBackendStatus();

    if (isOnline) {
        return null;
    }

    return (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-background/95 backdrop-blur-sm">
            <div className="mx-4 max-w-md rounded-xl border border-destructive/30 bg-card p-8 shadow-2xl">
                <div className="flex flex-col items-center text-center">
                    <div className="mb-4 flex h-16 w-16 items-center justify-center rounded-full bg-destructive/10">
                        <AlertCircle className="h-8 w-8 text-destructive" />
                    </div>

                    <h2 className="mb-2 text-xl font-semibold text-foreground">
                        Backend Not Available
                    </h2>

                    <p className="mb-6 text-muted-foreground">
                        {error === "Backend is not running" ? (
                            <>
                                The Scriber backend service is not running. Please start the application
                                or check if the backend process is active.
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
