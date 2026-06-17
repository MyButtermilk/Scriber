"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { Loader2, Square } from "lucide-react";
import { listen } from "@tauri-apps/api/event";
import { apiUrl, isTauriRuntime, loadBackendBaseUrlFromTauri, wsUrl } from "@/lib/backend";
import {
  isScriberWebSocketMessage,
  type ScriberWebSocketMessage,
} from "@/contexts/WebSocketContext";

type OverlayMode = "hidden" | "initializing" | "recording" | "transcribing";

type OverlayEventPayload = {
  apiVersion?: string;
  renderer?: string;
  mode?: string;
  visible?: boolean;
};

const BAR_COUNT = 36;
const WAVEFORM_CANVAS_WIDTH = 162;
const WAVEFORM_CANVAS_HEIGHT = 29;
const PILL_PADDING = 5;
const STOP_BUTTON_SIZE = 31;
const STOP_ICON_SIZE = 12;
const OVERLAY_CONTENT_WIDTH = STOP_BUTTON_SIZE + WAVEFORM_CANVAS_WIDTH;
const PILL_WIDTH = OVERLAY_CONTENT_WIDTH + PILL_PADDING * 2;
const PILL_HEIGHT = STOP_BUTTON_SIZE + PILL_PADDING * 2;
const MIDNIGHT_COLORS = ["#93C5FD", "#3B82F6", "#1E3A8A"];
const OVERLAY_RMS_NOISE_FLOOR = 0.00003;
const OVERLAY_RMS_DISPLAY_SCALE = 90;

function normalizeMode(value: unknown): OverlayMode {
  const mode = String(value || "").trim().toLowerCase();
  if (mode === "initializing" || mode === "recording" || mode === "transcribing") {
    return mode;
  }
  return "hidden";
}

function devOverlayModeFromLocation(): OverlayMode {
  if (typeof window === "undefined") return "hidden";
  const params = new URLSearchParams(window.location.search);
  return normalizeMode(params.get("overlayMode"));
}

function devOverlayRmsFromLocation(): number {
  if (typeof window === "undefined") return 0;
  const params = new URLSearchParams(window.location.search);
  const fallback = isTauriRuntime() ? 0 : 0.32;
  return Math.min(1, Math.max(0, Number(params.get("overlayRms")) || fallback));
}

function interpolateColor(colors: string[], factor: number): string {
  if (colors.length === 1) return colors[0];
  const clamped = Math.max(0, Math.min(1, factor));
  const idx = clamped * (colors.length - 1);
  const i = Math.floor(idx);
  const f = idx - i;
  if (i >= colors.length - 1) return colors[colors.length - 1];

  const c1 = parseInt(colors[i].slice(1), 16);
  const c2 = parseInt(colors[i + 1].slice(1), 16);
  const r1 = (c1 >> 16) & 255;
  const g1 = (c1 >> 8) & 255;
  const b1 = c1 & 255;
  const r2 = (c2 >> 16) & 255;
  const g2 = (c2 >> 8) & 255;
  const b2 = c2 & 255;
  const r = Math.round(r1 + (r2 - r1) * f);
  const g = Math.round(g1 + (g2 - g1) * f);
  const b = Math.round(b1 + (b2 - b1) * f);
  return `rgb(${r}, ${g}, ${b})`;
}

function overlayVisualizerLevelFromRms(rms: number): number {
  const level = Math.min(1, Math.max(0, Number(rms) || 0));
  if (level <= OVERLAY_RMS_NOISE_FLOOR) {
    return 0;
  }
  return Math.min(1, Math.pow((level - OVERLAY_RMS_NOISE_FLOOR) * OVERLAY_RMS_DISPLAY_SCALE, 0.72));
}

function OverlayWaveform({ active, rmsRef }: { active: boolean; rmsRef: { current: number } }) {
  const canvasRef = useRef<HTMLCanvasElement | null>(null);
  const levelsRef = useRef<number[]>(Array(BAR_COUNT).fill(0));
  const displayRef = useRef<number[]>(Array(BAR_COUNT).fill(0.12));
  const fallRef = useRef<number[]>(Array(BAR_COUNT).fill(0));

  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas || !active) return;

    let rafId = 0;
    let lastFrameAt = performance.now();
    let lastDrawAt = 0;
    const gravity = 0.8;
    const riseSpeed = 0.6;

    const draw = (now: number) => {
      if (now - lastDrawAt < 33) {
        rafId = requestAnimationFrame(draw);
        return;
      }
      lastDrawAt = now;
      const rect = canvas.getBoundingClientRect();
      const dpr = Math.min(window.devicePixelRatio || 1, 2);
      const width = Math.max(1, Math.round(rect.width * dpr));
      const height = Math.max(1, Math.round(rect.height * dpr));
      if (canvas.width !== width || canvas.height !== height) {
        canvas.width = width;
        canvas.height = height;
      }

      const ctx = canvas.getContext("2d");
      if (!ctx) {
        rafId = requestAnimationFrame(draw);
        return;
      }

      const inputLevel = overlayVisualizerLevelFromRms(rmsRef.current);
      for (let i = 0; i < BAR_COUNT; i++) {
        const center = BAR_COUNT / 2;
        const dist = Math.abs(i - center) / center;
        const freqFactor = 1.0 - dist * dist * 0.6;
        const phase = i * 0.52 + now * 0.01;
        const wave = inputLevel > 0 ? 0.84 + 0.16 * Math.sin(phase) : 0;
        levelsRef.current[i] = inputLevel * freqFactor * wave;
      }

      const dt = Math.min(0.034, Math.max(0.001, (now - lastFrameAt) / 1000));
      lastFrameAt = now;
      const levels = levelsRef.current;
      const display = displayRef.current;
      const fall = fallRef.current;
      const padLeft = 8 * dpr;
      const padRight = 10 * dpr;
      const usableWidth = Math.max(1, width - padLeft - padRight);
      const gap = 1.8 * dpr;
      const barWidth = Math.max(1.8 * dpr, (usableWidth - gap * (BAR_COUNT - 1)) / BAR_COUNT);
      const centerY = height / 2;
      const maxHeight = 24 * dpr;

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
  }, [active, rmsRef]);

  return (
    <canvas
      data-testid="native-recording-waveform"
      ref={canvasRef}
      width={WAVEFORM_CANVAS_WIDTH}
      height={WAVEFORM_CANVAS_HEIGHT}
      aria-hidden="true"
      style={{
        width: WAVEFORM_CANVAS_WIDTH,
        height: WAVEFORM_CANVAS_HEIGHT,
        display: "block",
      }}
    />
  );
}

function StatusContent({ mode }: { mode: "initializing" | "transcribing" }) {
  const label = mode === "initializing" ? "Preparing..." : "Transcribing...";
  const color = mode === "initializing" ? "text-blue-300" : "text-blue-400";
  return (
    <div className={`flex h-full w-full items-center justify-center gap-1.5 ${color}`}>
      <Loader2 className="h-3.5 w-3.5 animate-spin" />
      <span className="text-[12px] font-medium leading-none">{label}</span>
    </div>
  );
}

function overlayLayerClass(active: boolean): string {
  return [
    "absolute inset-0 flex items-center",
    "transition-all duration-200 ease-[cubic-bezier(0.16,1,0.3,1)]",
    active ? "translate-y-0 scale-100 opacity-100" : "pointer-events-none translate-y-1 scale-[0.98] opacity-0",
  ].join(" ");
}

export default function NativeRecordingOverlay() {
  const [backendReady, setBackendReady] = useState(!isTauriRuntime());
  const [mode, setMode] = useState<OverlayMode>(() => devOverlayModeFromLocation());
  const rmsRef = useRef(devOverlayRmsFromLocation());
  const activeSessionIdRef = useRef<string | null>(null);
  const isDevOverlayPreview = !isTauriRuntime() && devOverlayModeFromLocation() !== "hidden";
  const visible = mode !== "hidden";

  const applyWsMessage = useCallback((msg: ScriberWebSocketMessage) => {
    const msgSessionId = typeof msg.sessionId === "string" ? msg.sessionId : null;
    const activeSessionId = activeSessionIdRef.current;
    if (msgSessionId && activeSessionId && msgSessionId !== activeSessionId) {
      return;
    }

    switch (msg.type) {
      case "audio_level":
        rmsRef.current = Math.min(1, Math.max(0, Number(msg.rms) || 0));
        break;
      case "state":
      case "status":
        if (msgSessionId && !activeSessionId) {
          activeSessionIdRef.current = msgSessionId;
        }
        if (msg.recordingState === "finalizing" || msg.transcribing) {
          setMode("transcribing");
        } else if (msg.recordingState === "recording" || msg.listening) {
          setMode("recording");
        } else if (msg.recordingState === "initializing") {
          setMode("initializing");
        }
        break;
      case "session_started":
        if (msgSessionId) {
          activeSessionIdRef.current = msgSessionId;
        }
        rmsRef.current = 0;
        setMode("initializing");
        break;
      case "transcribing":
        setMode("transcribing");
        break;
      case "session_finished":
      case "error":
        activeSessionIdRef.current = null;
        setMode("hidden");
        break;
    }
  }, []);

  useEffect(() => {
    document.documentElement.dataset.scriberOverlayWindow = "true";
    document.body.dataset.scriberOverlayWindow = "true";
    return () => {
      delete document.documentElement.dataset.scriberOverlayWindow;
      delete document.body.dataset.scriberOverlayWindow;
    };
  }, []);

  useEffect(() => {
    let cancelled = false;
    void loadBackendBaseUrlFromTauri().finally(() => {
      if (!cancelled) {
        setBackendReady(true);
      }
    });
    return () => {
      cancelled = true;
    };
  }, []);

  useEffect(() => {
    if (!isTauriRuntime()) return;
    let unlisten: (() => void) | undefined;
    let disposed = false;
    void listen<OverlayEventPayload>("scriber-overlay-state", (event) => {
      const payload = event.payload || {};
      const nextMode = payload.visible === false ? "hidden" : normalizeMode(payload.mode);
      setMode(nextMode);
    })
      .then((cleanup) => {
        if (disposed) {
          cleanup();
        } else {
          unlisten = cleanup;
        }
      })
      .catch((error) => console.debug("Native overlay event listener failed.", error));
    return () => {
      disposed = true;
      unlisten?.();
    };
  }, []);

  useEffect(() => {
    if (!backendReady || isDevOverlayPreview) return;
    const socket = new WebSocket(wsUrl("/ws"));
    socket.onmessage = (event) => {
      try {
        const data = JSON.parse(String(event.data));
        if (isScriberWebSocketMessage(data)) {
          applyWsMessage(data);
        }
      } catch {
        // Ignore malformed diagnostic traffic.
      }
    };
    return () => {
      socket.close();
    };
  }, [applyWsMessage, backendReady, isDevOverlayPreview]);

  const handleStop = useCallback(async () => {
    try {
      await fetch(apiUrl("/api/live-mic/stop"), {
        method: "POST",
        credentials: "include",
      });
    } catch {
      // The backend state stream will hide the overlay if stop already won.
    }
  }, []);

  return (
    <div className="flex h-screen w-screen items-center justify-center bg-transparent">
      {visible && (
        <div className="relative inline-flex">
          <div
            data-testid="native-recording-shadow"
            aria-hidden="true"
            className="absolute inset-0 bg-slate-950/30"
            style={{
              borderRadius: 9999,
              filter: "blur(12px)",
              pointerEvents: "none",
              transform: "translateY(7px) scaleX(0.96)",
            }}
          />
          <div
            data-testid="native-recording-pill"
            className="relative flex items-center bg-black"
            style={{
              borderRadius: 9999,
              padding: PILL_PADDING,
              overflow: "hidden",
              boxShadow: "inset 0 0 0 1px rgba(255, 255, 255, 0.10)",
              width: PILL_WIDTH,
              height: PILL_HEIGHT,
            }}
          >
            <div
              className="relative overflow-hidden"
              style={{
                width: OVERLAY_CONTENT_WIDTH,
                height: STOP_BUTTON_SIZE,
              }}
            >
              <div className={overlayLayerClass(mode === "initializing")} aria-hidden={mode !== "initializing"}>
                <StatusContent mode="initializing" />
              </div>
              <div className={`${overlayLayerClass(mode === "recording")} gap-0`} aria-hidden={mode !== "recording"}>
                <button
                  data-testid="native-recording-stop"
                  type="button"
                  tabIndex={mode === "recording" ? 0 : -1}
                  onClick={handleStop}
                  className="flex shrink-0 items-center justify-center border-0 bg-[#e74c3c] text-white transition-colors duration-150 hover:bg-[#f05242]"
                  style={{
                    width: STOP_BUTTON_SIZE,
                    height: STOP_BUTTON_SIZE,
                    borderRadius: STOP_BUTTON_SIZE / 2,
                  }}
                  aria-label="Stop recording"
                >
                  <Square className="fill-current" style={{ width: STOP_ICON_SIZE, height: STOP_ICON_SIZE }} />
                </button>
                <OverlayWaveform active={mode === "recording"} rmsRef={rmsRef} />
              </div>
              <div className={overlayLayerClass(mode === "transcribing")} aria-hidden={mode !== "transcribing"}>
                <StatusContent mode="transcribing" />
              </div>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
