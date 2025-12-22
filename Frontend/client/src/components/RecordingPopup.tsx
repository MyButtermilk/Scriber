"use client";

import React, { useEffect, useRef, useState, memo } from "react";
import { motion, AnimatePresence } from "framer-motion";
import { Square, Loader2 } from "lucide-react";
import { wsUrl, apiUrl } from "@/lib/backend";

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
                            type: "spring",
                            stiffness: 300,
                            damping: 20,
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
    const [isRecording, setIsRecording] = useState(false);
    const [isTranscribing, setIsTranscribing] = useState(false);
    const [audioLevels, setAudioLevels] = useState<number[]>(Array(BAR_COUNT).fill(0.12));
    const wsRef = useRef<WebSocket | null>(null);
    const audioLevelsRef = useRef<number[]>(Array(BAR_COUNT).fill(0.12));

    useEffect(() => {
        const ws = new WebSocket(wsUrl("/ws"));
        wsRef.current = ws;

        ws.onmessage = (event) => {
            try {
                const msg = JSON.parse(event.data);
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
                        audioLevelsRef.current = Array(BAR_COUNT).fill(0.12);
                        setAudioLevels([...audioLevelsRef.current]);
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
                    case "audio_level":
                        const rms = Math.min(1, Math.max(0, Number(msg.rms) || 0));
                        const normalizedLevel = Math.pow(rms, 0.25) * 0.88 + 0.12;
                        audioLevelsRef.current = [...audioLevelsRef.current.slice(1), normalizedLevel];
                        setAudioLevels([...audioLevelsRef.current]);
                        break;
                }
            } catch {
                // ignore parse errors
            }
        };

        return () => {
            try {
                ws.close();
            } catch {
                // ignore
            }
        };
    }, []);

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
        <AnimatePresence>
            {isVisible && (
                <motion.div
                    initial={{ opacity: 0, y: 100, scale: 0.8 }}
                    animate={{ opacity: 1, y: 0, scale: 1 }}
                    exit={{ opacity: 0, y: 50, scale: 0.9 }}
                    transition={{
                        type: "spring",
                        stiffness: 400,
                        damping: 30,
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
                            boxShadow: '0 10px 40px rgba(0, 0, 0, 0.5)',
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
