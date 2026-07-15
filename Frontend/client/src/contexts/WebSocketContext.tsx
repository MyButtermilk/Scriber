import React, { createContext, useContext, useEffect, useRef, useState, useCallback, useMemo } from "react";
import {
    BACKEND_SESSION_TOKEN_REQUIRED_EVENT,
    backendSessionToken,
    isBackendSessionTokenRequired,
    reportFrontendReady,
    wsUrl,
} from "@/lib/backend";
import type { MicrophoneDevice } from "@/lib/api-types";
import type { MeetingNote, MeetingSegment, MeetingSummary, MeetingTranscriptCheckpoint } from "@/lib/api-types";

type BaseWsMessage = {
    apiVersion: string;
    sessionId?: string | null;
};

type TranscriptSession = {
    id?: string | number;
    content?: string;
    [key: string]: unknown;
};

type InputWarningAction = {
    id: string;
    label: string;
    uri: string;
};

type ModelDownloadStatus = "downloading" | "ready" | "error" | "not_downloaded";

export type ScriberWebSocketMessage =
    | (BaseWsMessage & {
        type: "state";
        listening: boolean;
        voiceEnrollmentActive: boolean;
        status: string;
        inputWarning?: string;
        inputWarningCode?: string;
        inputWarningActions?: InputWarningAction[];
        current?: TranscriptSession | null;
        backgroundProcessing: boolean;
        recordingState: string;
        transcribing: boolean;
    })
    | (BaseWsMessage & {
        type: "status";
        status: string;
        listening: boolean;
        recordingState?: string;
        transcribing?: boolean;
        inputWarning?: string;
        inputWarningCode?: string;
        inputWarningActions?: InputWarningAction[];
    })
    | (BaseWsMessage & { type: "audio_level"; rms: number })
    | (BaseWsMessage & {
        type: "input_warning";
        active: boolean;
        message: string;
        code?: string;
        actions?: InputWarningAction[];
    })
    | (BaseWsMessage & { type: "transcript"; text: string; content?: string; isFinal: boolean })
    | (BaseWsMessage & {
        type: "error";
        message: string;
        title?: string;
        provider?: string;
        providerLabel?: string;
        category?: string;
        code?: string;
        retryable?: boolean;
    })
    | (BaseWsMessage & {
        type: "history_updated";
        transcriptId?: string;
        transcriptType?: "mic" | "file" | "youtube" | string;
        status?: string;
        step?: string;
        summaryStatus?: string;
        updatedAt?: string;
        reason?: string;
    })
    | (BaseWsMessage & {
        type: "frontend_performance_flush";
        sourceInstanceId: string;
        heartbeatSequence: number;
    })
    | (BaseWsMessage & { type: "transcribing" })
    | (BaseWsMessage & { type: "session_started"; session: TranscriptSession })
    | (BaseWsMessage & { type: "session_finished"; session: TranscriptSession })
    | (BaseWsMessage & {
        type: "microphones_updated";
        devices: MicrophoneDevice[];
        favoriteMicRestored: boolean;
        restoredDeviceId?: string;
        restoredDeviceLabel?: string;
    })
    | (BaseWsMessage & { type: "settings_updated" })
    | (BaseWsMessage & {
        type: "onnx_download_progress";
        modelId: string;
        quantization?: string;
        progress: number;
        status: ModelDownloadStatus;
        message?: string;
    })
    | (BaseWsMessage & { type: "onnx_models_updated"; modelId: string })
    | (BaseWsMessage & { type: "meeting_state"; meeting: MeetingSummary })
    | (BaseWsMessage & { type: "meeting_segment"; meetingId: string; segment: MeetingSegment })
    | (BaseWsMessage & { type: "meeting_checkpoint"; meetingId: string; checkpoint: MeetingTranscriptCheckpoint })
    | (BaseWsMessage & { type: "meeting_transcript_edited"; meetingId: string; segment: MeetingSegment; transcriptEditVersion: number; outputsStale: boolean })
    | (BaseWsMessage & { type: "meeting_note"; meetingId: string; note: MeetingNote })
    | (BaseWsMessage & { type: "meeting_audio_level"; meetingId: string; source: string; rms: number })
    | (BaseWsMessage & { type: "meeting_live_status"; meetingId: string; source: string; status: "reconnecting" | "recovered" | "degraded"; reconnectCount: number })
    | (BaseWsMessage & { type: "meeting_finalize_progress" | "meeting_analysis_progress"; meetingId: string; progress: number; status: string })
    | (BaseWsMessage & { type: "meeting_import_progress"; importId: string; phase: string; progress: number; status: string; receivedBytes: number; expectedBytes?: number; meetingId?: string })
    | (BaseWsMessage & { type: "meeting_detected"; detectionId: string; label: string; source: string; meetingId?: string })
    | (BaseWsMessage & { type: "meeting_chat_delta"; meetingId: string; threadId: string; delta: string })
    | (BaseWsMessage & { type: "meeting_delivery_updated"; meetingId: string; delivery: Record<string, unknown> });

type MessageHandler = (data: ScriberWebSocketMessage) => void;

export function isScriberWebSocketMessage(data: unknown): data is ScriberWebSocketMessage {
    if (!data || typeof data !== "object") {
        return false;
    }
    const candidate = data as { apiVersion?: unknown; type?: unknown };
    return typeof candidate.apiVersion === "string" && typeof candidate.type === "string";
}

interface WebSocketContextValue {
    /** Current connection status */
    isConnected: boolean;
    /** Number of reconnection attempts */
    reconnectCount: number;
    /** Subscribe to messages with a handler - returns unsubscribe function */
    subscribe: (handler: MessageHandler) => () => void;
    /** Send a message through the shared connection */
    send: (data: unknown) => void;
}

const WebSocketContext = createContext<WebSocketContextValue | null>(null);

interface WebSocketProviderProps {
    children: React.ReactNode;
    /** WebSocket endpoint path, e.g. "/ws" */
    path?: string;
    /** Start the connection only after backend readiness is known */
    enabled?: boolean;
    /** Enable auto-reconnection (default: true) */
    autoReconnect?: boolean;
    /** Base delay between reconnection attempts in ms (default: 1000) */
    reconnectDelay?: number;
    /** Maximum reconnection attempts before giving up (default: Infinity) */
    maxReconnectAttempts?: number;
}

/**
 * WebSocket Provider - Singleton connection shared across the app.
 *
 * PERFORMANCE OPTIMIZATION:
 * - Single WebSocket connection instead of 5+ per page
 * - Reduces server load and network overhead
 * - Eliminates connection setup latency when switching pages
 * - 200-400ms latency reduction on page navigation
 */
export function WebSocketProvider({
    children,
    path = "/ws",
    enabled = true,
    autoReconnect = true,
    reconnectDelay = 1000,
    maxReconnectAttempts = Infinity,
}: WebSocketProviderProps) {
    const wsRef = useRef<WebSocket | null>(null);
    const reconnectTimeoutRef = useRef<NodeJS.Timeout | null>(null);
    const reconnectCountRef = useRef(0);
    const shouldReconnectRef = useRef(autoReconnect);
    const subscribersRef = useRef<Set<MessageHandler>>(new Set());

    const [isConnected, setIsConnected] = useState(false);
    const [reconnectCount, setReconnectCount] = useState(0);

    const clearReconnectTimeout = useCallback(() => {
        if (reconnectTimeoutRef.current) {
            clearTimeout(reconnectTimeoutRef.current);
            reconnectTimeoutRef.current = null;
        }
    }, []);

    const closeCurrentSocket = useCallback(() => {
        const socket = wsRef.current;
        wsRef.current = null;
        socket?.close();
    }, []);

    const connect = useCallback(() => {
        clearReconnectTimeout();
        if (!enabled) {
            setIsConnected(false);
            closeCurrentSocket();
            return;
        }

        if (isBackendSessionTokenRequired() && !backendSessionToken) {
            setIsConnected(false);
            closeCurrentSocket();
            clearReconnectTimeout();
            return;
        }

        // Clean up existing connection
        closeCurrentSocket();

        const scheduleReconnect = () => {
            if (!shouldReconnectRef.current || reconnectCountRef.current >= maxReconnectAttempts) {
                return;
            }
            const delay = Math.min(
                reconnectDelay * Math.pow(1.5, reconnectCountRef.current),
                30000,
            );
            reconnectCountRef.current++;
            setReconnectCount(reconnectCountRef.current);
            reconnectTimeoutRef.current = setTimeout(() => {
                connect();
            }, delay);
        };

        try {
            const ws = new WebSocket(wsUrl(path));

            ws.onopen = () => {
                if (wsRef.current !== ws) {
                    ws.close();
                    return;
                }
                setIsConnected(true);
                reconnectCountRef.current = 0;
                setReconnectCount(0);
                void reportFrontendReady({ force: true }).catch((readyError) => {
                    console.debug("Frontend readiness beacon failed after WebSocket open.", readyError);
                });
            };

            ws.onmessage = (event) => {
                if (wsRef.current !== ws) {
                    return;
                }
                try {
                    const data = JSON.parse(event.data) as unknown;
                    if (!isScriberWebSocketMessage(data)) {
                        return;
                    }
                    // Broadcast to all subscribers
                    subscribersRef.current.forEach((handler) => {
                        try {
                            handler(data);
                        } catch (e) {
                            console.error("WebSocket handler error:", e);
                        }
                    });
                } catch {
                    // Ignore parse errors
                }
            };

            ws.onclose = () => {
                if (wsRef.current !== ws) {
                    return;
                }
                setIsConnected(false);
                wsRef.current = null;

                scheduleReconnect();
            };

            ws.onerror = (error) => {
                console.error("WebSocket error:", error);
            };

            wsRef.current = ws;
        } catch (error) {
            console.error("WebSocket connection error:", error);
            setIsConnected(false);
            scheduleReconnect();
        }
    }, [path, enabled, reconnectDelay, maxReconnectAttempts, clearReconnectTimeout, closeCurrentSocket]);

    const send = useCallback((data: unknown) => {
        if (wsRef.current?.readyState === WebSocket.OPEN) {
            wsRef.current.send(typeof data === "string" ? data : JSON.stringify(data));
        }
    }, []);

    const subscribe = useCallback((handler: MessageHandler) => {
        subscribersRef.current.add(handler);
        return () => {
            subscribersRef.current.delete(handler);
        };
    }, []);

    // Initial connection and cleanup
    useEffect(() => {
        shouldReconnectRef.current = autoReconnect && enabled;
        if (!enabled) {
            clearReconnectTimeout();
            closeCurrentSocket();
            setIsConnected(false);
            return;
        }

        connect();

        return () => {
            shouldReconnectRef.current = false;
            clearReconnectTimeout();
            closeCurrentSocket();
        };
    }, [connect, enabled, autoReconnect, clearReconnectTimeout, closeCurrentSocket]);

    useEffect(() => {
        const handleAuthStateChange = () => {
            if (!enabled) {
                return;
            }
            if (isBackendSessionTokenRequired() && !backendSessionToken) {
                shouldReconnectRef.current = false;
                clearReconnectTimeout();
                closeCurrentSocket();
                setIsConnected(false);
                shouldReconnectRef.current = autoReconnect;
                return;
            }
            shouldReconnectRef.current = autoReconnect;
            connect();
        };
        window.addEventListener(BACKEND_SESSION_TOKEN_REQUIRED_EVENT, handleAuthStateChange);
        return () => window.removeEventListener(BACKEND_SESSION_TOKEN_REQUIRED_EVENT, handleAuthStateChange);
    }, [enabled, autoReconnect, clearReconnectTimeout, closeCurrentSocket, connect]);

    const value: WebSocketContextValue = useMemo(() => ({
        isConnected,
        reconnectCount,
        subscribe,
        send,
    }), [isConnected, reconnectCount, subscribe, send]);

    return (
        <WebSocketContext.Provider value={value}>
            {children}
        </WebSocketContext.Provider>
    );
}

/**
 * Hook to access the shared WebSocket connection.
 *
 * Usage:
 * ```tsx
 * function MyComponent() {
 *     const { isConnected, subscribe, send } = useWebSocketContext();
 *
 *     useEffect(() => {
 *         return subscribe((msg) => {
 *             if (msg.type === "audio_level") {
 *                 // Handle audio level
 *             }
 *         });
 *     }, [subscribe]);
 * }
 * ```
 */
export function useWebSocketContext(): WebSocketContextValue {
    const context = useContext(WebSocketContext);
    if (!context) {
        throw new Error("useWebSocketContext must be used within WebSocketProvider");
    }
    return context;
}

/**
 * Hook for subscribing to WebSocket messages with automatic cleanup.
 *
 * PERFORMANCE: Uses the singleton WebSocket connection, eliminating
 * per-component connection overhead.
 *
 * @param onMessage - Callback for incoming messages
 */
export function useSharedWebSocket(onMessage: MessageHandler): { isConnected: boolean; send: (data: unknown) => void } {
    const { isConnected, subscribe, send } = useWebSocketContext();
    const onMessageRef = useRef(onMessage);

    // Keep callback ref updated
    useEffect(() => {
        onMessageRef.current = onMessage;
    }, [onMessage]);

    // Subscribe with stable callback
    useEffect(() => {
        return subscribe((data) => {
            onMessageRef.current(data);
        });
    }, [subscribe]);

    return { isConnected, send };
}
