"use client";

import React, { useEffect, useRef, useState, memo, useCallback } from "react";
import { motion, AnimatePresence } from "framer-motion";
import { Square, Loader2 } from "lucide-react";
import { wsUrl, apiUrl } from "@/lib/backend";
import { useToast } from "@/hooks/use-toast";
import { useWebSocket } from "@/hooks/use-websocket";

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

// Custom mirrored waveform that responds to WebSocket audio levels
// Memoized to prevent re-renders when parent state changes
const AudioWaveform = memo(function AudioWaveform({ audioLevels }: { audioLevels: number[] }) {
    return (
        <div
            style={{
                display: 'flex',
                alignItems: 'center',
                justifyContent: 'center',
                height: 48,
                gap: 2,
                paddingLeft: 16,
                paddingRight: 24,
            }}
        >
            {audioLevels.map((level, index) => {
                // Apply center-weighted variation for natural look
                const centerFactor = 1.0 - Math.abs(index - audioLevels.length / 2) / (audioLevels.length / 2);
                const adjustedLevel = level * (0.5 + 0.5 * centerFactor);
                const barHeight = Math.max(2, adjustedLevel * 40);

                return (
                    <motion.div
                        key={index}
                        animate={{
                            height: barHeight,
                        }}
                        transition={{
                            duration: 0,
                        }}
                        style={{
                            width: 2.5,
                            borderRadius: 1,
                            // Color based on height (bright when tall, dark when short)
                            backgroundColor: interpolateColor(MIDNIGHT_COLORS, 1.0 - Math.min(1.0, adjustedLevel)),
                        }}
                    />
                );
            })}
        </div>
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
    const [audioLevels, setAudioLevels] = useState<number[]>(Array(BAR_COUNT).fill(0.12));

    // CAVA-style state refs
    const levelsRef = useRef<number[]>(Array(BAR_COUNT).fill(0));  // Target levels
    const displayRef = useRef<number[]>(Array(BAR_COUNT).fill(0.12));  // Displayed levels
    const fallRef = useRef<number[]>(Array(BAR_COUNT).fill(0));  // Fall velocities
    const agcRef = useRef(0.02);  // Auto-gain control
    const animFrameRef = useRef<number | null>(null);

    // CAVA-style animation loop
    useEffect(() => {
        const gravity = 0.8;
        const riseSpeed = 0.6;

        const animate = () => {
            const levels = levelsRef.current;
            const display = displayRef.current;
            const fall = fallRef.current;
            let changed = false;

            for (let i = 0; i < BAR_COUNT; i++) {
                const target = levels[i];
                const current = display[i];

                if (target > current) {
                    // Fast rise
                    display[i] = current + (target - current) * riseSpeed;
                    fall[i] = 0;
                    changed = true;
                } else if (current > 0.12) {
                    // Gravity fall
                    fall[i] += gravity * 0.016;
                    display[i] = Math.max(0.12, current - fall[i]);
                    changed = true;
                }
            }

            if (changed) {
                setAudioLevels([...display]);
            }

            animFrameRef.current = requestAnimationFrame(animate);
        };

        animFrameRef.current = requestAnimationFrame(animate);

        return () => {
            if (animFrameRef.current) {
                cancelAnimationFrame(animFrameRef.current);
            }
        };
    }, []);

    // WebSocket message handler with error support
    const handleWsMessage = useCallback((msg: any) => {
        if (!msg || typeof msg !== "object") return;

        switch (msg.type) {
            case "state":
            case "status":
                setIsRecording(!!msg.listening);
                if (msg.transcribing !== undefined) {
                    setIsTranscribing(!!msg.transcribing);
                }
                break;
            case "session_started":
                setIsRecording(true);
                setIsTranscribing(false);
                // Reset all levels
                levelsRef.current = Array(BAR_COUNT).fill(0);
                displayRef.current = Array(BAR_COUNT).fill(0.12);
                fallRef.current = Array(BAR_COUNT).fill(0);
                agcRef.current = 0.02;
                break;
            case "transcribing":
                // Recording stopped, now transcribing
                setIsRecording(false);
                setIsTranscribing(true);
                break;
            case "session_finished":
                // Transcription complete
                setIsRecording(false);
                setIsTranscribing(false);
                break;
            case "error":
                // Handle recording errors - hide popup and show error toast
                setIsRecording(false);
                setIsTranscribing(false);
                toast({
                    title: "Recording Error",
                    description: msg.message || "An error occurred during recording.",
                    variant: "destructive",
                    duration: 6000,
                });
                break;
            case "audio_level":
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

    // WebSocket with auto-reconnection
    useWebSocket({
        path: "/ws",
        onMessage: handleWsMessage,
        autoReconnect: true,
        reconnectDelay: 1000,
    });

    const handleStop = async () => {
        try {
            await fetch(apiUrl("/api/live-mic/stop"), {
                method: "POST",
                credentials: "include",
            });
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
                            <AudioWaveform audioLevels={audioLevels} />
                        ) : isTranscribing ? (
                            <TranscribingText />
                        ) : null}
                    </div>
                </motion.div>
            )}
        </AnimatePresence>
    );
}
