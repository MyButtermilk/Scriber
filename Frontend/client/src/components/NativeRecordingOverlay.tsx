"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { Loader2, Square } from "lucide-react";
import { listen } from "@tauri-apps/api/event";
import {
  isTauriRuntime,
  loadBackendBaseUrlFromTauri,
  setTrayRecordingState,
  wsUrl,
} from "@/lib/backend";
import { requestLiveMicStop } from "@/lib/live-mic-control";
import {
  DEFAULT_VISUALIZER_BAR_COUNT,
  loadVisualizerBarCount,
  normalizeVisualizerBarCount,
} from "@/lib/visualizer-settings";
import {
  isScriberWebSocketMessage,
  type ScriberWebSocketMessage,
} from "@/contexts/WebSocketContext";
import { useI18n } from "@/i18n";

type OverlayMode = "hidden" | "initializing" | "recording" | "transcribing";

type OverlayEventPayload = {
  apiVersion?: string;
  renderer?: string;
  mode?: string;
  visible?: boolean;
  rms?: number;
  lastRms?: number;
};

const WAVEFORM_CANVAS_WIDTH = 162;
const WAVEFORM_CANVAS_HEIGHT = 29;
const PILL_PADDING = 5;
const STOP_BUTTON_SIZE = 31;
const STOP_ICON_SIZE = 12;
const OVERLAY_CONTENT_WIDTH = STOP_BUTTON_SIZE + WAVEFORM_CANVAS_WIDTH;
const PILL_WIDTH = OVERLAY_CONTENT_WIDTH + PILL_PADDING * 2;
const PILL_HEIGHT = STOP_BUTTON_SIZE + PILL_PADDING * 2;
const PILL_RADIUS = PILL_HEIGHT / 2;
const OVERLAY_DROP_SHADOW =
  "0 14px 26px -14px rgba(15, 23, 42, 0.62), 0 6px 14px -12px rgba(15, 23, 42, 0.38)";
const OVERLAY_INSET_SHADOW = "inset 0 0 0 1px rgba(255, 255, 255, 0.10)";
const OVERLAY_PILL_SHADOW = `${OVERLAY_DROP_SHADOW}, ${OVERLAY_INSET_SHADOW}`;
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

function modeFromNativeOverlayState(payload: OverlayEventPayload | null | undefined): OverlayMode {
  if (!payload || payload.visible === false) return "hidden";
  return normalizeMode(payload.mode);
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

function resizeBarBuffer(values: number[], count: number, fill: number): number[] {
  if (values.length === count) {
    return values;
  }
  const next = values.slice(0, count);
  while (next.length < count) {
    next.push(fill);
  }
  return next;
}

function OverlayWaveform({
  active,
  rmsRef,
  barCount,
}: {
  active: boolean;
  rmsRef: { current: number };
  barCount: number;
}) {
  const canvasRef = useRef<HTMLCanvasElement | null>(null);
  const resolvedBarCount = normalizeVisualizerBarCount(barCount);
  const levelsRef = useRef<number[]>(Array(resolvedBarCount).fill(0));
  const displayRef = useRef<number[]>(Array(resolvedBarCount).fill(0.12));
  const fallRef = useRef<number[]>(Array(resolvedBarCount).fill(0));

  useEffect(() => {
    levelsRef.current = resizeBarBuffer(levelsRef.current, resolvedBarCount, 0);
    displayRef.current = resizeBarBuffer(displayRef.current, resolvedBarCount, 0.12);
    fallRef.current = resizeBarBuffer(fallRef.current, resolvedBarCount, 0);
  }, [resolvedBarCount]);

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
      for (let i = 0; i < resolvedBarCount; i++) {
        const center = resolvedBarCount / 2;
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
      const gap = Math.max(
        0.5 * dpr,
        Math.min(1.8 * dpr, usableWidth / Math.max(1, resolvedBarCount * 7)),
      );
      const barWidth = Math.max(
        0.7 * dpr,
        (usableWidth - gap * (resolvedBarCount - 1)) / resolvedBarCount,
      );
      const centerY = height / 2;
      const maxHeight = 24 * dpr;

      ctx.clearRect(0, 0, width, height);
      for (let i = 0; i < resolvedBarCount; i++) {
        const target = levels[i] || 0;
        const current = display[i] || 0.12;
        if (target > current) {
          display[i] = current + (target - current) * riseSpeed;
          fall[i] = 0;
        } else if (current > 0.12) {
          fall[i] = (fall[i] || 0) + gravity * dt;
          display[i] = Math.max(0.12, current - fall[i]);
        }
        const centerFactor = 1.0 - Math.abs(i - resolvedBarCount / 2) / (resolvedBarCount / 2);
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
  }, [active, resolvedBarCount, rmsRef]);

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
  const { t } = useI18n();
  const label = mode === "initializing" ? t("Preparing...") : t("Transcribing...");
  const color = mode === "initializing" ? "text-blue-300" : "text-blue-400";
  return (
    <div className={`flex h-full w-full items-center justify-center gap-1.5 ${color}`}>
      <Loader2 className="h-3.5 w-3.5 animate-spin motion-reduce:animate-none" />
      <span className="text-[12px] font-medium leading-none">{label}</span>
    </div>
  );
}

function overlayLayerClass(active: boolean): string {
  return [
    "absolute inset-0 flex items-center",
    "transition-[opacity,filter] duration-[var(--duration-quick)] ease-[var(--ease-smooth-out)] motion-reduce:transition-none",
    active ? "opacity-100 blur-0" : "pointer-events-none opacity-0 blur-[2px]",
  ].join(" ");
}

export default function NativeRecordingOverlay() {
  const { t } = useI18n();
  const [backendReady, setBackendReady] = useState(!isTauriRuntime());
  const [mode, setMode] = useState<OverlayMode>(() => devOverlayModeFromLocation());
  const [visualizerBarCount, setVisualizerBarCount] = useState(DEFAULT_VISUALIZER_BAR_COUNT);
  const rmsRef = useRef(devOverlayRmsFromLocation());
  const activeSessionIdRef = useRef<string | null>(null);
  const isDevOverlayPreview = !isTauriRuntime() && devOverlayModeFromLocation() !== "hidden";
  const visible = mode !== "hidden";

  const refreshVisualizerBarCount = useCallback(async (signal?: AbortSignal) => {
    try {
      const count = await loadVisualizerBarCount(signal);
      setVisualizerBarCount(count);
    } catch (e: any) {
      if (e?.name !== "AbortError") {
        setVisualizerBarCount(DEFAULT_VISUALIZER_BAR_COUNT);
      }
    }
  }, []);

  useEffect(() => {
    const controller = new AbortController();
    void refreshVisualizerBarCount(controller.signal);
    return () => controller.abort();
  }, [refreshVisualizerBarCount]);

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
        // In the desktop runtime, only the native overlay event may hide the
        // renderer. A terminal WebSocket message does not prove that the
        // always-on-top native window has completed its physical hide.
        if (!isTauriRuntime()) {
          setMode("hidden");
        }
        break;
      case "settings_updated":
        void refreshVisualizerBarCount();
        break;
    }
  }, [refreshVisualizerBarCount]);

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
    let receivedNativeEvent = false;
    void listen<OverlayEventPayload>("scriber-overlay-state", (event) => {
      receivedNativeEvent = true;
      if (Number.isFinite(event.payload.rms)) {
        rmsRef.current = Math.min(1, Math.max(0, Number(event.payload.rms)));
      }
      setMode(modeFromNativeOverlayState(event.payload));
    })
      .then(async (cleanup) => {
        if (disposed) {
          cleanup();
          return;
        }

        // Register the event listener first, then reconcile the authoritative native snapshot.
        // The WebView is pre-created while hidden, so the first hotkey event may otherwise arrive
        // while this lazy-loaded component is still mounting and leave a transparent window.
        unlisten = cleanup;
        try {
          const { invoke } = await import("@tauri-apps/api/core");
          const snapshot = await invoke<OverlayEventPayload>("native_overlay_renderer_ready");
          if (!disposed && Number.isFinite(snapshot.lastRms)) {
            rmsRef.current = Math.min(1, Math.max(0, Number(snapshot.lastRms)));
          }
          if (!disposed && !receivedNativeEvent) {
            setMode(modeFromNativeOverlayState(snapshot));
          }
        } catch (error) {
          console.debug("Native overlay renderer handshake failed.", error);
        }
      })
      .catch((error) => console.debug("Native overlay event listener failed.", error));
    return () => {
      disposed = true;
      unlisten?.();
    };
  }, []);

  useEffect(() => {
    if (!isTauriRuntime()) return;
    const active = mode === "initializing" || mode === "recording";
    const trayMode = visible ? mode : "idle";
    void setTrayRecordingState(active, trayMode).catch((error) => {
      console.debug("Native overlay tray state sync failed.", error);
    });
  }, [mode, visible]);

  useEffect(() => {
    if (!backendReady || isDevOverlayPreview) return;
    let disposed = false;
    let socket: WebSocket | null = null;
    let reconnectTimer: number | null = null;

    const connect = () => {
      if (disposed) return;
      socket = new WebSocket(wsUrl("/ws"));
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
      socket.onerror = () => {
        socket?.close();
      };
      socket.onclose = () => {
        if (disposed) return;
        reconnectTimer = window.setTimeout(connect, 750);
      };
    };

    connect();
    return () => {
      disposed = true;
      if (reconnectTimer !== null) {
        window.clearTimeout(reconnectTimer);
      }
      socket?.close();
    };
  }, [applyWsMessage, backendReady, isDevOverlayPreview]);

  const handleStop = useCallback(async () => {
    try {
      await requestLiveMicStop();
    } catch {
      // The authoritative native transition will hide the overlay if stop already won.
    }
  }, []);

  return (
    <div className="flex h-screen w-screen items-center justify-center bg-transparent">
      {visible && (
        <div className="relative inline-flex">
          <div
            data-testid="native-recording-pill"
            className="relative flex items-center bg-black"
            style={{
              borderRadius: PILL_RADIUS,
              padding: PILL_PADDING,
              overflow: "hidden",
              boxShadow: OVERLAY_PILL_SHADOW,
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
                  aria-label={t("Stop recording")}
                >
                  <Square className="fill-current" style={{ width: STOP_ICON_SIZE, height: STOP_ICON_SIZE }} />
                </button>
                <OverlayWaveform
                  active={mode === "recording"}
                  rmsRef={rmsRef}
                  barCount={visualizerBarCount}
                />
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
