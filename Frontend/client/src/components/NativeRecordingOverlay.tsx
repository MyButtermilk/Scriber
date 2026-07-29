"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { Square } from "lucide-react";
import { listen } from "@tauri-apps/api/event";
import { isTauriRuntime, loadBackendBaseUrlFromTauri, setTrayRecordingState, wsUrl } from "@/lib/backend";
import { requestLiveMicStop } from "@/lib/live-mic-control";
import MicrophoneEnergyField from "@/components/MicrophoneEnergyField";
import type { OverlayVisualizerStyle } from "@/lib/api-types";
import { overlayVisualizerLevelFromRms } from "@/lib/native-overlay-visualizer";
import {
  DEFAULT_OVERLAY_VISUALIZER_STYLE,
  DEFAULT_VISUALIZER_BAR_COUNT,
  loadVisualizerSettings,
  normalizeOverlayVisualizerStyle,
  normalizeVisualizerBarCount,
} from "@/lib/visualizer-settings";
import { isScriberWebSocketMessage, type ScriberWebSocketMessage } from "@/contexts/WebSocketContext";
import { useI18n } from "@/i18n";
import { TranscriptionShimmerText } from "@/components/ui/transcription-shimmer-text";
import { WavePhysicsLoader } from "@/components/ui/wave-physics-loader";

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
const ENERGY_WAVE_PLOT_X = 0;
const ENERGY_WAVE_PLOT_Y = (PILL_HEIGHT - WAVEFORM_CANVAS_HEIGHT) / 2;
const ENERGY_WAVE_PLOT_WIDTH = PILL_WIDTH;
const OVERLAY_DROP_SHADOW = "0 7px 15px -6px rgba(7, 19, 31, 0.42), 0 15px 28px -15px rgba(7, 19, 31, 0.30)";
const OVERLAY_INSET_SHADOW = "inset 0 0 0 1px rgba(255, 255, 255, 0.10)";
const ENERGY_PILL_BACKGROUND = [
  "radial-gradient(ellipse 72% 175% at 66% 50%, rgba(40, 91, 132, 0.62) 0%, rgba(22, 57, 86, 0.30) 52%, rgba(7, 19, 31, 0) 100%)",
  "linear-gradient(90deg, #07131f 0%, #0a1c2d 28%, #0d263b 62%, #071522 100%)",
].join(", ");
const MIDNIGHT_COLORS = ["#93C5FD", "#3B82F6", "#1E3A8A"];
const BAR_FRAME_INTERVAL_MS = 1000 / 60;
const BAR_FRAME_DEADLINE_TOLERANCE_MS = 1.5;
const BAR_MAX_DEVICE_PIXEL_RATIO = 3;
const BAR_COLOR_STEPS = 128;
const BAR_IDLE_LEVEL = 0.06;
const NATIVE_RMS_FALLBACK_AFTER_MS = 250;

function normalizeMode(value: unknown): OverlayMode {
  const mode = String(value || "")
    .trim()
    .toLowerCase();
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
  const configured = params.get("overlayRms");
  const rms = configured === null ? fallback : Number(configured);
  return Math.min(1, Math.max(0, Number.isFinite(rms) ? rms : fallback));
}

function devOverlayStyleOverrideFromLocation(): OverlayVisualizerStyle | null {
  if (typeof window === "undefined") return null;
  const value = new URLSearchParams(window.location.search).get("overlayStyle");
  return value === "bars" || value === "energy_wave" ? value : null;
}

function devOverlayStyleFromLocation(): OverlayVisualizerStyle {
  return devOverlayStyleOverrideFromLocation() ?? DEFAULT_OVERLAY_VISUALIZER_STYLE;
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

const MIDNIGHT_PALETTE = Array.from({ length: BAR_COLOR_STEPS }, (_, index) =>
  interpolateColor(MIDNIGHT_COLORS, index / (BAR_COLOR_STEPS - 1)),
);

function OverlayBarWaveform({
  active,
  rmsRef,
  barCount,
  width,
  height,
}: {
  active: boolean;
  rmsRef: { current: number };
  barCount: number;
  width: number;
  height: number;
}) {
  const canvasRef = useRef<HTMLCanvasElement | null>(null);
  const resolvedBarCount = normalizeVisualizerBarCount(barCount);

  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas || !active) return;
    const ctx = canvas.getContext("2d", { alpha: true });
    if (!ctx) return;

    const levels = new Float32Array(resolvedBarCount);
    const display = new Float32Array(resolvedBarCount);
    const fall = new Float32Array(resolvedBarCount);
    display.fill(BAR_IDLE_LEVEL);
    let rafId = 0;
    let lastFrameAt = performance.now();
    let nextFrameAt = lastFrameAt;
    let devicePixelRatio = 1;
    const gravity = 0.8;
    const riseSpeed = 0.6;

    const syncCanvasSize = () => {
      devicePixelRatio = Math.min(window.devicePixelRatio || 1, BAR_MAX_DEVICE_PIXEL_RATIO);
      const pixelWidth = Math.max(1, Math.round(width * devicePixelRatio));
      const pixelHeight = Math.max(1, Math.round(height * devicePixelRatio));
      if (canvas.width !== pixelWidth || canvas.height !== pixelHeight) {
        canvas.width = pixelWidth;
        canvas.height = pixelHeight;
      }
      ctx.setTransform(devicePixelRatio, 0, 0, devicePixelRatio, 0, 0);
    };

    syncCanvasSize();
    window.addEventListener("resize", syncCanvasSize);

    const draw = (now: number) => {
      rafId = requestAnimationFrame(draw);
      if (now + BAR_FRAME_DEADLINE_TOLERANCE_MS < nextFrameAt) {
        return;
      }

      const inputLevel = overlayVisualizerLevelFromRms(rmsRef.current);
      for (let i = 0; i < resolvedBarCount; i++) {
        const center = resolvedBarCount / 2;
        const dist = Math.abs(i - center) / center;
        const freqFactor = 1.0 - dist * dist * 0.6;
        const phase = i * 0.52 + now * 0.01;
        const wave = inputLevel > 0 ? 0.84 + 0.16 * Math.sin(phase) : 0;
        levels[i] = inputLevel * freqFactor * wave;
      }

      const dt = Math.min(0.05, Math.max(0.001, (now - lastFrameAt) / 1000));
      lastFrameAt = now;
      const padLeft = 0;
      const padRight = 0;
      const usableWidth = Math.max(1, width - padLeft - padRight);
      const gap = Math.max(0.5, Math.min(1.8, usableWidth / Math.max(1, resolvedBarCount * 7)));
      const barWidth = Math.max(0.7, (usableWidth - gap * (resolvedBarCount - 1)) / resolvedBarCount);
      const centerY = height / 2;
      const maxHeight = Math.min(24, height - PILL_PADDING * 2);

      ctx.setTransform(1, 0, 0, 1, 0, 0);
      ctx.clearRect(0, 0, canvas.width, canvas.height);
      ctx.setTransform(devicePixelRatio, 0, 0, devicePixelRatio, 0, 0);
      for (let i = 0; i < resolvedBarCount; i++) {
        const target = levels[i] || 0;
        const current = display[i] || BAR_IDLE_LEVEL;
        if (target > current) {
          display[i] = current + (target - current) * riseSpeed;
          fall[i] = 0;
        } else if (current > BAR_IDLE_LEVEL) {
          fall[i] = (fall[i] || 0) + gravity * dt;
          display[i] = Math.max(BAR_IDLE_LEVEL, current - fall[i]);
        }
        const centerFactor = 1.0 - Math.abs(i - resolvedBarCount / 2) / (resolvedBarCount / 2);
        const adjustedLevel = display[i] * (0.5 + 0.5 * centerFactor);
        const barHeight = Math.max(1 / devicePixelRatio, adjustedLevel * maxHeight);
        const x = padLeft + i * (barWidth + gap);
        const y = centerY - barHeight / 2;
        const radius = Math.min(barWidth / 2, 2);
        const paletteIndex = Math.round((1 - Math.min(1, adjustedLevel)) * (BAR_COLOR_STEPS - 1));
        ctx.fillStyle = MIDNIGHT_PALETTE[paletteIndex];
        ctx.beginPath();
        ctx.roundRect(x, y, barWidth, barHeight, radius);
        ctx.fill();
      }

      do {
        nextFrameAt += BAR_FRAME_INTERVAL_MS;
      } while (nextFrameAt <= now + BAR_FRAME_DEADLINE_TOLERANCE_MS);
    };

    rafId = requestAnimationFrame(draw);
    return () => {
      cancelAnimationFrame(rafId);
      window.removeEventListener("resize", syncCanvasSize);
    };
  }, [active, height, resolvedBarCount, rmsRef, width]);

  return (
    <canvas
      data-testid="native-recording-waveform"
      ref={canvasRef}
      data-render-profile="typed-array-60hz-hidpi-bars"
      width={width}
      height={height}
      aria-hidden="true"
      style={{
        position: "absolute",
        inset: 0,
        width,
        height,
        display: "block",
        borderRadius: height / 2,
        pointerEvents: "none",
      }}
    />
  );
}

function StatusContent({
  mode,
  visualizerStyle,
}: {
  mode: "initializing" | "transcribing";
  visualizerStyle: OverlayVisualizerStyle;
}) {
  const { t } = useI18n();
  const label = mode === "initializing" ? t("Preparing...") : t("Transcribing...");

  if (mode === "transcribing") {
    return (
      <div className="flex h-full w-full items-center justify-center">
        <TranscriptionShimmerText
          tone={visualizerStyle === "energy_wave" ? "energy" : "blue"}
          className="text-[12px] font-medium leading-none"
        >
          {label}
        </TranscriptionShimmerText>
      </div>
    );
  }

  return (
    <div className="flex h-full w-full items-center justify-center gap-1.5 text-blue-300">
      <WavePhysicsLoader size="inline" theme="dark" />
      <span className="text-[12px] font-medium leading-none">{label}</span>
    </div>
  );
}

function overlayLayerClass(active: boolean): string {
  return ["absolute inset-0 flex items-center", active ? "opacity-100" : "pointer-events-none opacity-0"].join(" ");
}

export default function NativeRecordingOverlay() {
  const { t } = useI18n();
  const [backendReady, setBackendReady] = useState(!isTauriRuntime());
  const [mode, setMode] = useState<OverlayMode>(() => devOverlayModeFromLocation());
  const [visualizerBarCount, setVisualizerBarCount] = useState(DEFAULT_VISUALIZER_BAR_COUNT);
  const [overlayVisualizerStyle, setOverlayVisualizerStyle] = useState<OverlayVisualizerStyle>(() =>
    devOverlayStyleFromLocation(),
  );
  const rmsRef = useRef(devOverlayRmsFromLocation());
  const activeSessionIdRef = useRef<string | null>(null);
  const lastWsRmsAtRef = useRef(Number.NEGATIVE_INFINITY);
  const visualizerSettingsRequestRef = useRef<AbortController | null>(null);
  const isDevOverlayPreview = !isTauriRuntime() && devOverlayModeFromLocation() !== "hidden";
  const visible = mode !== "hidden";
  const energyWaveSelected = overlayVisualizerStyle === "energy_wave";
  const energyWaveActive = mode === "recording" && overlayVisualizerStyle === "energy_wave";
  const barsActive = mode === "recording" && overlayVisualizerStyle === "bars";

  const refreshVisualizerSettings = useCallback(async () => {
    visualizerSettingsRequestRef.current?.abort();
    const controller = new AbortController();
    visualizerSettingsRequestRef.current = controller;
    try {
      const settings = await loadVisualizerSettings(controller.signal);
      if (controller.signal.aborted) return;
      setVisualizerBarCount(settings.barCount);
      const devOverride = isTauriRuntime() ? null : devOverlayStyleOverrideFromLocation();
      setOverlayVisualizerStyle(devOverride ?? normalizeOverlayVisualizerStyle(settings.overlayStyle));
    } catch (error) {
      if ((error as { name?: string })?.name === "AbortError") return;
      // Keep the last known-good renderer choice through transient reconnects.
    } finally {
      if (visualizerSettingsRequestRef.current === controller) {
        visualizerSettingsRequestRef.current = null;
      }
    }
  }, []);

  useEffect(() => {
    if (!backendReady || isDevOverlayPreview) return;
    void refreshVisualizerSettings();
    return () => visualizerSettingsRequestRef.current?.abort();
  }, [backendReady, isDevOverlayPreview, refreshVisualizerSettings]);

  const applyWsMessage = useCallback(
    (msg: ScriberWebSocketMessage) => {
      const msgSessionId = typeof msg.sessionId === "string" ? msg.sessionId : null;
      const activeSessionId = activeSessionIdRef.current;
      if (msgSessionId && activeSessionId && msgSessionId !== activeSessionId) {
        return;
      }

      switch (msg.type) {
        case "audio_level":
          rmsRef.current = Math.min(1, Math.max(0, Number(msg.rms) || 0));
          lastWsRmsAtRef.current = performance.now();
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
          void refreshVisualizerSettings();
          break;
      }
    },
    [refreshVisualizerSettings],
  );

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
      const wsRmsIsStale = performance.now() - lastWsRmsAtRef.current >= NATIVE_RMS_FALLBACK_AFTER_MS;
      if (wsRmsIsStale && Number.isFinite(event.payload.rms)) {
        rmsRef.current = Math.min(1, Math.max(0, Number(event.payload.rms)));
      }
      const nextMode = modeFromNativeOverlayState(event.payload);
      setMode((currentMode) => (currentMode === nextMode ? currentMode : nextMode));
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
      socket.onopen = () => {
        void refreshVisualizerSettings();
      };
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
  }, [applyWsMessage, backendReady, isDevOverlayPreview, refreshVisualizerSettings]);

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
        <div className="relative inline-flex" style={{ isolation: "isolate" }}>
          <div
            aria-hidden="true"
            className="native-recording-pill-shadow absolute inset-0"
            style={{
              zIndex: 0,
              borderRadius: PILL_RADIUS,
              background: "rgba(7, 19, 31, 0.18)",
              boxShadow: OVERLAY_DROP_SHADOW,
              transform: "translateY(5px) scaleX(0.96) scaleY(0.86)",
              pointerEvents: "none",
            }}
          />
          <div
            data-testid="native-recording-pill"
            data-visualizer-style={overlayVisualizerStyle}
            className="native-recording-pill relative flex items-center"
            style={{
              zIndex: 1,
              borderRadius: PILL_RADIUS,
              padding: PILL_PADDING,
              overflow: "hidden",
              boxShadow: OVERLAY_INSET_SHADOW,
              width: PILL_WIDTH,
              height: PILL_HEIGHT,
              background: energyWaveSelected ? ENERGY_PILL_BACKGROUND : "#000",
            }}
          >
            {energyWaveActive && (
              <MicrophoneEnergyField
                active
                rmsRef={rmsRef}
                width={PILL_WIDTH}
                height={PILL_HEIGHT}
                background={ENERGY_PILL_BACKGROUND}
                plotX={ENERGY_WAVE_PLOT_X}
                plotY={ENERGY_WAVE_PLOT_Y}
                plotWidth={ENERGY_WAVE_PLOT_WIDTH}
                plotHeight={WAVEFORM_CANVAS_HEIGHT}
              />
            )}
            {barsActive && (
              <OverlayBarWaveform
                active
                rmsRef={rmsRef}
                barCount={visualizerBarCount}
                width={PILL_WIDTH}
                height={PILL_HEIGHT}
              />
            )}
            <div
              className="relative z-10 overflow-hidden"
              style={{
                width: OVERLAY_CONTENT_WIDTH,
                height: STOP_BUTTON_SIZE,
              }}
            >
              <div className={overlayLayerClass(mode === "initializing")} aria-hidden={mode !== "initializing"}>
                <StatusContent mode="initializing" visualizerStyle={overlayVisualizerStyle} />
              </div>
              <div className={`${overlayLayerClass(mode === "recording")} gap-0`} aria-hidden={mode !== "recording"}>
                <button
                  data-testid="native-recording-stop"
                  type="button"
                  tabIndex={mode === "recording" ? 0 : -1}
                  onClick={handleStop}
                  className="native-recording-stop relative z-10 flex shrink-0 items-center justify-center border-0 bg-[#e74c3c] text-white hover:bg-[#f05242]"
                  style={{
                    width: STOP_BUTTON_SIZE,
                    height: STOP_BUTTON_SIZE,
                    borderRadius: STOP_BUTTON_SIZE / 2,
                  }}
                  aria-label={t("Stop recording")}
                >
                  <Square className="fill-current" style={{ width: STOP_ICON_SIZE, height: STOP_ICON_SIZE }} />
                </button>
                <div
                  data-testid="native-recording-visualizer-space"
                  aria-hidden="true"
                  style={{
                    width: WAVEFORM_CANVAS_WIDTH,
                    height: WAVEFORM_CANVAS_HEIGHT,
                  }}
                />
              </div>
              <div className={overlayLayerClass(mode === "transcribing")} aria-hidden={mode !== "transcribing"}>
                <StatusContent mode="transcribing" visualizerStyle={overlayVisualizerStyle} />
              </div>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
