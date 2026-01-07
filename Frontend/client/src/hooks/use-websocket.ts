import { useEffect, useRef, useCallback, useState } from "react";
import { wsUrl } from "@/lib/backend";

export interface UseWebSocketOptions {
    /** WebSocket endpoint path, e.g. "/ws" */
    path: string;
    /** Callback for incoming messages */
    onMessage?: (data: any) => void;
    /** Callback when connection opens */
    onOpen?: () => void;
    /** Callback when connection closes */
    onClose?: () => void;
    /** Callback when connection errors */
    onError?: (error: Event) => void;
    /** Enable auto-reconnection (default: true) */
    autoReconnect?: boolean;
    /** Base delay between reconnection attempts in ms (default: 1000) */
    reconnectDelay?: number;
    /** Maximum reconnection attempts before giving up (default: Infinity) */
    maxReconnectAttempts?: number;
}

interface UseWebSocketReturn {
    /** Current connection status */
    isConnected: boolean;
    /** Number of reconnection attempts */
    reconnectCount: number;
    /** Manually send a message */
    send: (data: any) => void;
    /** Manually reconnect */
    reconnect: () => void;
    /** Close connection without auto-reconnect */
    close: () => void;
}

/**
 * Custom hook for WebSocket connections with auto-reconnection.
 * 
 * Features:
 * - Automatic reconnection with exponential backoff
 * - Connection state tracking
 * - Clean disconnect on unmount
 */
export function useWebSocket({
    path,
    onMessage,
    onOpen,
    onClose,
    onError,
    autoReconnect = true,
    reconnectDelay = 1000,
    maxReconnectAttempts = Infinity,
}: UseWebSocketOptions): UseWebSocketReturn {
    const wsRef = useRef<WebSocket | null>(null);
    const reconnectTimeoutRef = useRef<NodeJS.Timeout | null>(null);
    const reconnectCountRef = useRef(0);
    const shouldReconnectRef = useRef(autoReconnect);
    const [isConnected, setIsConnected] = useState(false);
    const [reconnectCount, setReconnectCount] = useState(0);

    // Store callbacks in refs to avoid effect re-runs
    const onMessageRef = useRef(onMessage);
    const onOpenRef = useRef(onOpen);
    const onCloseRef = useRef(onClose);
    const onErrorRef = useRef(onError);

    useEffect(() => {
        onMessageRef.current = onMessage;
        onOpenRef.current = onOpen;
        onCloseRef.current = onClose;
        onErrorRef.current = onError;
    }, [onMessage, onOpen, onClose, onError]);

    const clearReconnectTimeout = useCallback(() => {
        if (reconnectTimeoutRef.current) {
            clearTimeout(reconnectTimeoutRef.current);
            reconnectTimeoutRef.current = null;
        }
    }, []);

    const connect = useCallback(() => {
        // Clean up existing connection
        if (wsRef.current) {
            wsRef.current.close();
            wsRef.current = null;
        }

        try {
            const ws = new WebSocket(wsUrl(path));

            ws.onopen = () => {
                setIsConnected(true);
                reconnectCountRef.current = 0;
                setReconnectCount(0);
                onOpenRef.current?.();
            };

            ws.onmessage = (event) => {
                try {
                    const data = JSON.parse(event.data);
                    onMessageRef.current?.(data);
                } catch {
                    // Ignore parse errors
                }
            };

            ws.onclose = () => {
                setIsConnected(false);
                wsRef.current = null;
                onCloseRef.current?.();

                // Auto-reconnect with exponential backoff
                if (shouldReconnectRef.current && reconnectCountRef.current < maxReconnectAttempts) {
                    const delay = Math.min(
                        reconnectDelay * Math.pow(1.5, reconnectCountRef.current),
                        30000 // Max 30 seconds
                    );
                    reconnectCountRef.current++;
                    setReconnectCount(reconnectCountRef.current);

                    reconnectTimeoutRef.current = setTimeout(() => {
                        connect();
                    }, delay);
                }
            };

            ws.onerror = (error) => {
                onErrorRef.current?.(error);
            };

            wsRef.current = ws;
        } catch (error) {
            console.error("WebSocket connection error:", error);
        }
    }, [path, reconnectDelay, maxReconnectAttempts]);

    const send = useCallback((data: any) => {
        if (wsRef.current?.readyState === WebSocket.OPEN) {
            wsRef.current.send(typeof data === "string" ? data : JSON.stringify(data));
        }
    }, []);

    const reconnect = useCallback(() => {
        clearReconnectTimeout();
        reconnectCountRef.current = 0;
        setReconnectCount(0);
        shouldReconnectRef.current = true;
        connect();
    }, [connect, clearReconnectTimeout]);

    const close = useCallback(() => {
        shouldReconnectRef.current = false;
        clearReconnectTimeout();
        if (wsRef.current) {
            wsRef.current.close();
            wsRef.current = null;
        }
        setIsConnected(false);
    }, [clearReconnectTimeout]);

    // Initial connection and cleanup
    useEffect(() => {
        shouldReconnectRef.current = autoReconnect;
        connect();

        return () => {
            shouldReconnectRef.current = false;
            clearReconnectTimeout();
            if (wsRef.current) {
                wsRef.current.close();
                wsRef.current = null;
            }
        };
    }, [connect, autoReconnect, clearReconnectTimeout]);

    return {
        isConnected,
        reconnectCount,
        send,
        reconnect,
        close,
    };
}
