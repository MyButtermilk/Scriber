"use client";

import React, { useEffect, useRef, useState, memo, useCallback } from "react";
import { motion, AnimatePresence } from "framer-motion";
import { Square, Loader2 } from "lucide-react";
import { wsUrl, apiUrl } from "@/lib/backend";
import { fetchWithTimeout } from "@/lib/fetch-with-timeout";
import { useToast } from "@/hooks/use-toast";
import { useSharedWebSocket, type ScriberWebSocketMessage } from "@/contexts/WebSocketContext";
import { showRecordingErrorToast } from "@/lib/recording-error-toast";

const BAR_COUNT = 56; // ~30% reduction from 80

// MIDNIGHT theme colors (blue gradient) - matching backend
const MIDNIGHT_COLORS = ["#3B82F6", "#1E3A8A", "#172554"];

// Interpolate between gradient colors based on position
function interpolateColor(colors: string[], factor: number): string {
    if (colors.length === 1) return colors[0];
    factor = Math.max(0, Math.min(1, factor));
    const idx = factor * (colors.length - 1);
    const i = Math.floor(idx);
    const f = idx - i;
    if (i >= colors.length - 1) return colors[colors.length - 1];

    // Parse hex colors
    const c1 = parseInt(colors[i].slice(1), 16);
    const c2 = parseInt(colors[i + 1].slice(1), 16);

    const r1 = (c1 >> 16) & 255, g1 = (c1 >> 8) & 255, b1 = c1 & 255;
    const r2 = (c2 >> 16) & 255, g2 = (c2 >> 8) & 255, b2 = c2 & 255;

    const r = Math.round(r1 + (r2 - r1) * f);
    const g = Math.round(g1 + (g2 - g1) * f);
    const b = Math.round(b1 + (b2 - b1) * f);

    return `rgb(${r}, ${g}, ${b})`;
}

interface AudioWaveformProps {
    levelsRef: React.MutableRefObject<number[]>;
    displayRef: React.MutableRefObject<number[]>;
    fallRef: React.MutableRefObject<number[]>;
}

const AudioWaveform = memo(function AudioWaveform({ levelsRef, displayRef, fallRef }: AudioWaveformProps) {
    const canvasRef = useRef<HTMLCanvasElement | null>(null);

    useEffect(() => {
        const canvas = canvasRef.current;
        if (!canvas) return;

        let rafId = 0;
        let lastFrameAt = performance.now();
        const gravity = 0.8;
        const riseSpeed = 0.6;

        const resize = () => {
            const rect = canvas.getBoundingClientRect();
            const dpr = Math.min(window.devicePixelRatio || 1, 2);
            const width = Math.max(1, Math.round(rect.width * dpr));
            const height = Math.max(1, Math.round(rect.height * dpr));
            if (canvas.width !== width || canvas.height !== height) {
                canvas.width = width;
                canvas.height = height;
            }
        };

        const draw = (now: number) => {
            resize();
            const ctx = canvas.getContext("2d");
            if (!ctx) {
                rafId = requestAnimationFrame(draw);
                return;
            }

            const dt = Math.min(0.034, Math.max(0.001, (now - lastFrameAt) / 1000));
            lastFrameAt = now;
            const levels = levelsRef.current;
            const display = displayRef.current;
            const fall = fallRef.current;
            const width = canvas.width;
            const height = canvas.height;
            const dpr = Math.min(window.devicePixelRatio || 1, 2);
            const padLeft = 16 * dpr;
            const padRight = 24 * dpr;
            const usableWidth = Math.max(1, width - padLeft - padRight);
            const gap = 2 * dpr;
            const barWidth = Math.max(2, (usableWidth - gap * (BAR_COUNT - 1)) / BAR_COUNT);
            const centerY = height / 2;
            const maxHeight = 40 * dpr;

            ctx.clearRect(0, 0, width, height);

            for (let i = 0; i < BAR_COUNT; i++) {
                const target = levels[i] || 0;
                const current = display[i] || 0.12;

                if (target > current) {
                    display[i] = current + (target - current) * riseSpeed;
                    fall[i] = 0;
                } else if (current > 0.12) {
                    fall[i] = (fall[i] || 0) + gravity * dt;
                    display[i] = Math.max(0.12, current - fall[i]);
                }

                const centerFactor = 1.0 - Math.abs(i - BAR_COUNT / 2) / (BAR_COUNT / 2);
                const adjustedLevel = display[i] * (0.5 + 0.5 * centerFactor);
                const barHeight = Math.max(2 * dpr, adjustedLevel * maxHeight);
                const x = padLeft + i * (barWidth + gap);
                const y = centerY - barHeight / 2;
                const radius = Math.min(barWidth / 2, 2 * dpr);

                ctx.fillStyle = interpolateColor(MIDNIGHT_COLORS, 1.0 - Math.min(1.0, adjustedLevel));
                ctx.beginPath();
                ctx.roundRect(x, y, barWidth, barHeight, radius);
                ctx.fill();
            }

            rafId = requestAnimationFrame(draw);
        };

        rafId = requestAnimationFrame(draw);
        return () => cancelAnimationFrame(rafId);
    }, [displayRef, fallRef, levelsRef]);

    return (
        <canvas
            ref={canvasRef}
            width={270}
            height={48}
            style={{
                width: 270,
                height: 48,
                display: "block",
            }}
            aria-hidden="true"
        />
    );
});

// Transcribing text with animated dots
function TranscribingText() {
    return (
        <motion.div
            initial={{ opacity: 0 }}
            animate={{ opacity: 1 }}
            style={{
                display: 'flex',
                alignItems: 'center',
                justifyContent: 'center',
                height: 48,
                paddingLeft: 24,
                paddingRight: 32,
                gap: 10,
            }}
        >
            <Loader2
                style={{
                    width: 20,
                    height: 20,
                    color: '#3B82F6',
                    animation: 'spin 1s linear infinite',
                }}
            />
            <span
                style={{
                    color: '#3B82F6',
                    fontSize: 16,
                    fontWeight: 500,
                    fontFamily: 'system-ui, -apple-system, sans-serif',
                    letterSpacing: '0.02em',
                }}
            >
                Transcribing...
            </span>
            <style>{`
                @keyframes spin {
                    from { transform: rotate(0deg); }
                    to { transform: rotate(360deg); }
                }
            `}</style>
        </motion.div>
    );
}

interface RecordingPopupProps {
    className?: string;
}

export function RecordingPopup({ className }: RecordingPopupProps) {
    const { toast } = useToast();
    const [isRecording, setIsRecording] = useState(false);
    const [isTranscribing, setIsTranscribing] = useState(false);
    const activeSessionIdRef = useRef<string | null>(null);

    // CAVA-style state refs
    const levelsRef = useRef<number[]>(Array(BAR_COUNT).fill(0));  // Target levels
    const displayRef = useRef<number[]>(Array(BAR_COUNT).fill(0.12));  // Displayed levels
    const fallRef = useRef<number[]>(Array(BAR_COUNT).fill(0));  // Fall velocities
    const agcRef = useRef(0.02);  // Auto-gain control

    // WebSocket message handler with error support
    const handleWsMessage = useCallback((msg: ScriberWebSocketMessage) => {
        if (!msg || typeof msg !== "object") return;
        const msgSessionId = typeof msg.sessionId === "string" ? msg.sessionId : null;
        const activeSessionId = activeSessionIdRef.current;

        switch (msg.type) {
            case "state":
            case "status":
                if (msgSessionId && activeSessionId && msgSessionId !== activeSessionId) {
                    break;
                }
                if (msgSessionId && !activeSessionId) {
                    activeSessionIdRef.current = msgSessionId;
                } else if (!msgSessionId && !msg.listening) {
                    activeSessionIdRef.current = null;
                }
                setIsRecording(!!msg.listening);
                if (msg.transcribing !== undefined) {
                    setIsTranscribing(!!msg.transcribing);
                }
                break;
            case "session_started":
                if (msgSessionId) {
                    activeSessionIdRef.current = msgSessionId;
                }
                setIsRecording(true);
                setIsTranscribing(false);
                // Reset all levels
                levelsRef.current = Array(BAR_COUNT).fill(0);
                displayRef.current = Array(BAR_COUNT).fill(0.12);
                fallRef.current = Array(BAR_COUNT).fill(0);
                agcRef.current = 0.02;
                break;
            case "transcribing":
                if (msgSessionId && activeSessionId && msgSessionId !== activeSessionId) {
                    break;
                }
                // Recording stopped, now transcribing
                setIsRecording(false);
                setIsTranscribing(true);
                break;
            case "session_finished":
                if (msgSessionId && activeSessionId && msgSessionId !== activeSessionId) {
                    break;
                }
                activeSessionIdRef.current = null;
                // Transcription complete
                setIsRecording(false);
                setIsTranscribing(false);
                break;
            case "error":
                if (msgSessionId && activeSessionId && msgSessionId !== activeSessionId) {
                    break;
                }
                // Handle recording errors - hide popup and show error toast
                setIsRecording(false);
                setIsTranscribing(false);
                showRecordingErrorToast(toast, msg);
                break;
            case "audio_level":
                if (msgSessionId && activeSessionId && msgSessionId !== activeSessionId) {
                    break;
                }
                const rms = Math.min(1, Math.max(0, Number(msg.rms) || 0));

                // Fast AGC like CAVA
                if (rms > agcRef.current) {
                    agcRef.current = rms;
                } else {
                    agcRef.current = agcRef.current * 0.98 + rms * 0.02;
                }

                // Normalize and apply power curve (25% gain boost)
                const norm = Math.pow(rms / (agcRef.current + 1e-6), 0.55) * 1.25;

                // Distribute across bars with frequency-like pattern
                for (let i = 0; i < BAR_COUNT; i++) {
                    const center = BAR_COUNT / 2;
                    const dist = Math.abs(i - center) / center;
                    const freqFactor = 1.0 - (dist * dist * 0.6);
                    const phase = i * 0.4 + rms * 20;
                    const wave = 0.85 + 0.15 * Math.sin(phase);
                    levelsRef.current[i] = norm * freqFactor * wave;
                }
                break;
        }
    }, [toast]);

    // PERFORMANCE: Uses singleton WebSocket connection (shared across all pages)
    useSharedWebSocket(handleWsMessage);

    const handleStop = async () => {
        try {
            await fetchWithTimeout(apiUrl("/api/live-mic/stop"), {
                method: "POST",
                credentials: "include",
            }, 15_000);
        } catch {
            // ignore errors
        }
    };

    const isVisible = isRecording || isTranscribing;

    return (
        <AnimatePresence mode="wait">
            {isVisible && (
                <motion.div
                    initial={{ opacity: 0, y: 40, scale: 0.95 }}
                    animate={{ opacity: 1, y: 0, scale: 1 }}
                    exit={{ opacity: 0, y: 20, scale: 0.98 }}
                    transition={{
                        duration: 0.2,
                        ease: [0.25, 0.46, 0.45, 0.94], // easeOutQuad for smooth feel
                    }}
                    style={{
                        willChange: 'transform, opacity',
                        transform: 'translateZ(0)', // Force GPU acceleration
                    }}
                    className={`fixed bottom-8 left-1/2 -translate-x-1/2 z-[9999] ${className || ""}`}
                >
                    {/* Pill-shaped container */}
                    <div
                        style={{
                            display: 'flex',
                            alignItems: 'center',
                            backgroundColor: '#000000',
                            borderRadius: 9999,
                            padding: '8px 8px 8px 8px',
                            boxShadow: '0 12px 36px rgba(0, 0, 0, 0.35)',
                        }}
                    >
                        {/* Stop Button - only show during recording */}
                        {isRecording && (
                            <button
                                onClick={handleStop}
                                style={{
                                    width: 52,
                                    height: 52,
                                    borderRadius: '50%',
                                    backgroundColor: '#e74c3c',
                                    border: 'none',
                                    cursor: 'pointer',
                                    display: 'flex',
                                    alignItems: 'center',
                                    justifyContent: 'center',
                                    flexShrink: 0,
                                    transition: 'transform 0.15s ease',
                                }}
                                onMouseEnter={(e) => e.currentTarget.style.transform = 'scale(1.05)'}
                                onMouseLeave={(e) => e.currentTarget.style.transform = 'scale(1)'}
                                aria-label="Stop recording"
                            >
                                <Square
                                    style={{
                                        width: 20,
                                        height: 20,
                                        color: 'white',
                                        fill: 'white',
                                    }}
                                />
                            </button>
                        )}

                        {/* Content: Waveform during recording, "Transcribing..." after */}
                        {isRecording ? (
                            <AudioWaveform levelsRef={levelsRef} displayRef={displayRef} fallRef={fallRef} />
                        ) : isTranscribing ? (
                            <TranscribingText />
                        ) : null}
                    </div>
                </motion.div>
            )}
        </AnimatePresence>
    );
}
