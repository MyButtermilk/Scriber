"use client";

import { useEffect, useRef } from "react";

import {
  ENERGY_WAVE_FOCUS,
  ENERGY_WAVE_SAMPLE_COUNT,
  ENERGY_WAVE_STRAND_COUNT,
  advanceReducedMotionEnvelope,
  advanceVisualizerEnvelope,
  energyWaveStrandVisibility,
  fillEnergyWaveYCoordinates,
  overlayVisualizerLevelFromRms,
} from "@/lib/native-overlay-visualizer";

type MicrophoneEnergyFieldProps = {
  active: boolean;
  rmsRef: { current: number };
  width: number;
  height: number;
  background: string;
  plotX: number;
  plotY: number;
  plotWidth: number;
  plotHeight: number;
};

const NORMAL_FRAME_INTERVAL_MS = 1000 / 60;
const REDUCED_MOTION_FRAME_INTERVAL_MS = 100;
const REDUCED_MOTION_SETTLED_FRAME_INTERVAL_MS = 250;
const REDUCED_MOTION_PHASE = 0.72;
const FRAME_DEADLINE_TOLERANCE_MS = 1.5;
const MAX_DEVICE_PIXEL_RATIO = 3;
const SETTLED_ENVELOPE_EPSILON = 0.002;
const ENERGY_WAVE_PATH_OVERSCAN = 8;
const WAVE_HALO_WIDTH = 1.35;
const WAVE_HALO_ALPHA = 0.04;
const WAVE_CORE_PHYSICAL_PX = 1;
const WAVE_CORE_ALPHA = 0.9;

function traceSmoothWave(
  ctx: CanvasRenderingContext2D,
  values: Float32Array,
  valueOffset: number,
  sampleCount: number,
  plotX: number,
  plotY: number,
  plotWidth: number,
): void {
  const stepX = plotWidth / (sampleCount - 1);
  const firstY = plotY + values[valueOffset];
  ctx.beginPath();
  ctx.moveTo(plotX - ENERGY_WAVE_PATH_OVERSCAN, firstY);
  ctx.lineTo(plotX, firstY);

  for (let index = 1; index < sampleCount - 1; index += 1) {
    const x = plotX + index * stepX;
    const nextX = x + stepX;
    const y = plotY + values[valueOffset + index];
    const nextY = plotY + values[valueOffset + index + 1];
    ctx.quadraticCurveTo(x, y, (x + nextX) / 2, (y + nextY) / 2);
  }

  const lastY = plotY + values[valueOffset + sampleCount - 1];
  ctx.lineTo(plotX + plotWidth, lastY);
  ctx.lineTo(plotX + plotWidth + ENERGY_WAVE_PATH_OVERSCAN, lastY);
}

export default function MicrophoneEnergyField({
  active,
  rmsRef,
  width,
  height,
  background,
  plotX,
  plotY,
  plotWidth,
  plotHeight,
}: MicrophoneEnergyFieldProps) {
  const canvasRef = useRef<HTMLCanvasElement | null>(null);

  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas || !active) return;
    // WebView2 may promote the low-latency canvas path into an opaque
    // compositor surface inside transparent Tauri windows. Keep the normal
    // alpha-composed 2D path so the pill gradient and rounded transparent
    // corners survive in the installed overlay.
    const ctx = canvas.getContext("2d", { alpha: true });
    if (!ctx) return;

    const coordinates = new Float32Array(ENERGY_WAVE_STRAND_COUNT * ENERGY_WAVE_SAMPLE_COUNT);
    const visibilities = new Float32Array(ENERGY_WAVE_STRAND_COUNT);
    let rafId = 0;
    let lastFrameAt = performance.now();
    let nextFrameAt = lastFrameAt;
    let envelope = 0;
    let reducedMotion = false;
    let devicePixelRatio = 1;
    let waveGradient: CanvasGradient;
    let focusGradient: CanvasGradient;

    const syncCanvasSize = () => {
      devicePixelRatio = Math.min(window.devicePixelRatio || 1, MAX_DEVICE_PIXEL_RATIO);
      const pixelWidth = Math.max(1, Math.round(width * devicePixelRatio));
      const pixelHeight = Math.max(1, Math.round(height * devicePixelRatio));
      if (canvas.width !== pixelWidth || canvas.height !== pixelHeight) {
        canvas.width = pixelWidth;
        canvas.height = pixelHeight;
      }

      ctx.setTransform(devicePixelRatio, 0, 0, devicePixelRatio, 0, 0);
      waveGradient = ctx.createLinearGradient(
        plotX - ENERGY_WAVE_PATH_OVERSCAN,
        0,
        plotX + plotWidth + ENERGY_WAVE_PATH_OVERSCAN,
        0,
      );
      waveGradient.addColorStop(0, "rgba(225, 179, 105, 0.82)");
      waveGradient.addColorStop(0.34, "#f6cf8c");
      waveGradient.addColorStop(ENERGY_WAVE_FOCUS, "#fff3dc");
      waveGradient.addColorStop(0.72, "#dfbd83");
      waveGradient.addColorStop(1, "rgba(195, 207, 205, 0.46)");

      const focusX = plotX + plotWidth * ENERGY_WAVE_FOCUS;
      const focusY = plotY + plotHeight / 2;
      focusGradient = ctx.createRadialGradient(focusX, focusY, 0, focusX, focusY, 5.2);
      focusGradient.addColorStop(0, "rgba(255, 246, 225, 0.92)");
      focusGradient.addColorStop(0.28, "rgba(246, 207, 140, 0.32)");
      focusGradient.addColorStop(1, "rgba(225, 179, 105, 0)");
    };

    const motionPreference = window.matchMedia("(prefers-reduced-motion: reduce)");
    const applyMotionPreference = () => {
      reducedMotion = motionPreference.matches;
      nextFrameAt = performance.now();
    };
    applyMotionPreference();
    syncCanvasSize();

    const draw = (now: number) => {
      rafId = requestAnimationFrame(draw);
      if (now + FRAME_DEADLINE_TOLERANCE_MS < nextFrameAt) {
        return;
      }

      const deltaMs = Math.min(100, Math.max(0, now - lastFrameAt));
      lastFrameAt = now;
      const target = overlayVisualizerLevelFromRms(rmsRef.current);
      envelope = reducedMotion
        ? advanceReducedMotionEnvelope(envelope, target, deltaMs)
        : advanceVisualizerEnvelope(envelope, target, deltaMs);

      const phase = reducedMotion ? REDUCED_MOTION_PHASE : now * 0.0082;
      for (let strandIndex = 0; strandIndex < ENERGY_WAVE_STRAND_COUNT; strandIndex += 1) {
        const valueOffset = strandIndex * ENERGY_WAVE_SAMPLE_COUNT;
        fillEnergyWaveYCoordinates(
          coordinates,
          valueOffset,
          plotHeight,
          ENERGY_WAVE_SAMPLE_COUNT,
          envelope,
          phase,
          strandIndex,
        );
        visibilities[strandIndex] = energyWaveStrandVisibility(strandIndex, envelope);
      }

      ctx.setTransform(1, 0, 0, 1, 0, 0);
      ctx.clearRect(0, 0, canvas.width, canvas.height);
      ctx.setTransform(devicePixelRatio, 0, 0, devicePixelRatio, 0, 0);
      ctx.lineCap = "round";
      ctx.lineJoin = "round";
      ctx.strokeStyle = waveGradient;

      ctx.globalCompositeOperation = "lighter";
      ctx.lineWidth = WAVE_HALO_WIDTH;
      for (let strandIndex = 0; strandIndex < ENERGY_WAVE_STRAND_COUNT; strandIndex += 1) {
        const visibility = visibilities[strandIndex];
        ctx.globalAlpha = visibility * WAVE_HALO_ALPHA * (0.44 + envelope * 0.56);
        traceSmoothWave(
          ctx,
          coordinates,
          strandIndex * ENERGY_WAVE_SAMPLE_COUNT,
          ENERGY_WAVE_SAMPLE_COUNT,
          plotX,
          plotY,
          plotWidth,
        );
        ctx.stroke();
      }

      ctx.globalCompositeOperation = "source-over";
      ctx.lineWidth = WAVE_CORE_PHYSICAL_PX / devicePixelRatio;
      for (let strandIndex = 0; strandIndex < ENERGY_WAVE_STRAND_COUNT; strandIndex += 1) {
        const visibility = visibilities[strandIndex];
        const isCore = strandIndex === Math.floor(ENERGY_WAVE_STRAND_COUNT / 2);
        ctx.globalAlpha = visibility * WAVE_CORE_ALPHA * (0.5 + envelope * 0.5) * (isCore ? 1 : 0.94);
        traceSmoothWave(
          ctx,
          coordinates,
          strandIndex * ENERGY_WAVE_SAMPLE_COUNT,
          ENERGY_WAVE_SAMPLE_COUNT,
          plotX,
          plotY,
          plotWidth,
        );
        ctx.stroke();
      }

      const focusX = plotX + plotWidth * ENERGY_WAVE_FOCUS;
      const focusY = plotY + plotHeight / 2;
      ctx.globalCompositeOperation = "lighter";
      ctx.globalAlpha = 0.18 + envelope * 0.34;
      ctx.fillStyle = focusGradient;
      ctx.beginPath();
      ctx.arc(focusX, focusY, 5.2, 0, Math.PI * 2);
      ctx.fill();
      ctx.globalAlpha = 1;
      ctx.globalCompositeOperation = "source-over";

      const reducedMotionSettled = reducedMotion && Math.abs(envelope - target) < SETTLED_ENVELOPE_EPSILON;
      const intervalMs = reducedMotionSettled
        ? REDUCED_MOTION_SETTLED_FRAME_INTERVAL_MS
        : reducedMotion
          ? REDUCED_MOTION_FRAME_INTERVAL_MS
          : NORMAL_FRAME_INTERVAL_MS;
      do {
        nextFrameAt += intervalMs;
      } while (nextFrameAt <= now + FRAME_DEADLINE_TOLERANCE_MS);
    };

    motionPreference.addEventListener("change", applyMotionPreference);
    window.addEventListener("resize", syncCanvasSize);
    rafId = requestAnimationFrame(draw);

    return () => {
      cancelAnimationFrame(rafId);
      motionPreference.removeEventListener("change", applyMotionPreference);
      window.removeEventListener("resize", syncCanvasSize);
    };
  }, [active, height, plotHeight, plotWidth, plotX, plotY, rmsRef, width]);

  return (
    <canvas
      data-testid="native-recording-waveform"
      data-visualizer-style="energy-wave-field"
      data-render-profile="typed-array-60hz-hidpi"
      ref={canvasRef}
      width={width}
      height={height}
      aria-hidden="true"
      style={{
        position: "absolute",
        inset: 0,
        width,
        height,
        display: "block",
        background,
        borderRadius: height / 2,
        clipPath: `inset(0 round ${height / 2}px)`,
        pointerEvents: "none",
      }}
    />
  );
}
