import { useEffect, useRef } from "react";

import {
  BLUE_FLAME_SAMPLE_COUNT,
  BLUE_FLAME_STRAND_COUNT,
  fillBlueFlameCoordinates,
} from "@/lib/blue-flame-visualizer";
import {
  advanceReducedMotionEnvelope,
  advanceVisualizerEnvelope,
  overlayVisualizerLevelFromRms,
} from "@/lib/native-overlay-visualizer";

export const BLUE_FLAME_PILL_BACKGROUND = "linear-gradient(105deg, #040c18, #081a30 58%, #061021)";

type MicrophoneBlueFlameProps = {
  active: boolean;
  rmsRef: { current: number };
  width: number;
  height: number;
};

/** Blue/cyan filaments adapted to the native pill, with a bounded Canvas 2D cost. */
export default function MicrophoneBlueFlame({ active, rmsRef, width, height }: MicrophoneBlueFlameProps) {
  const canvasRef = useRef<HTMLCanvasElement>(null);

  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas || !active) return;
    // Keep WebView2's normal alpha compositor; desynchronized canvases can
    // become opaque rectangular surfaces in the transparent overlay window.
    const ctx = canvas.getContext("2d", { alpha: true });
    if (!ctx) return;

    const coordinates = new Float32Array(BLUE_FLAME_STRAND_COUNT * BLUE_FLAME_SAMPLE_COUNT);
    const motion = window.matchMedia("(prefers-reduced-motion: reduce)");
    let reducedMotion = motion.matches;
    let pixelRatio = 1;
    let frameId = 0;
    let lastFrame = performance.now();
    let nextFrame = lastFrame;
    let envelope = 0;
    let background: CanvasGradient;
    let blue: CanvasGradient;
    let cyan: CanvasGradient;

    const resize = () => {
      pixelRatio = Math.min(3, Math.max(1, window.devicePixelRatio || 1));
      canvas.width = Math.max(1, Math.round(width * pixelRatio));
      canvas.height = Math.max(1, Math.round(height * pixelRatio));
      ctx.setTransform(pixelRatio, 0, 0, pixelRatio, 0, 0);
      background = ctx.createLinearGradient(0, 0, width, height * 0.3);
      background.addColorStop(0, "#040c18");
      background.addColorStop(0.58, "#081a30");
      background.addColorStop(1, "#061021");
      blue = ctx.createLinearGradient(0, height, width, 0);
      blue.addColorStop(0, "#267cff");
      blue.addColorStop(0.46, "#519dff");
      blue.addColorStop(1, "#185cea");
      cyan = ctx.createLinearGradient(0, 0, width, height);
      cyan.addColorStop(0, "#40a8ff");
      cyan.addColorStop(0.56, "#b5faff");
      cyan.addColorStop(1, "#32c7ec");
    };
    const changeMotion = () => {
      reducedMotion = motion.matches;
      nextFrame = performance.now();
    };
    const trace = (strand: number) => {
      const offset = strand * BLUE_FLAME_SAMPLE_COUNT;
      const step = width / (BLUE_FLAME_SAMPLE_COUNT - 1);
      ctx.beginPath();
      ctx.moveTo(-4, coordinates[offset]);
      for (let point = 0; point < BLUE_FLAME_SAMPLE_COUNT - 1; point += 1) {
        const x = point * step;
        ctx.quadraticCurveTo(
          x,
          coordinates[offset + point],
          x + step / 2,
          (coordinates[offset + point] + coordinates[offset + point + 1]) / 2,
        );
      }
      ctx.lineTo(width + 4, coordinates[offset + BLUE_FLAME_SAMPLE_COUNT - 1]);
    };
    const draw = (now: number) => {
      frameId = requestAnimationFrame(draw);
      if (now + 1.5 < nextFrame) return;
      const delta = Math.min(100, Math.max(0, now - lastFrame));
      lastFrame = now;
      const target = overlayVisualizerLevelFromRms(rmsRef.current);
      envelope = reducedMotion
        ? advanceReducedMotionEnvelope(envelope, target, delta)
        : advanceVisualizerEnvelope(envelope, target, delta);
      ctx.setTransform(1, 0, 0, 1, 0, 0);
      ctx.clearRect(0, 0, canvas.width, canvas.height);
      ctx.setTransform(pixelRatio, 0, 0, pixelRatio, 0, 0);
      ctx.save();
      ctx.beginPath();
      ctx.roundRect(0, 0, width, height, height / 2);
      ctx.clip();
      ctx.globalCompositeOperation = "source-over";
      ctx.globalAlpha = 1;
      ctx.fillStyle = background;
      ctx.fillRect(0, 0, width, height);

      if (envelope > 0.002) {
        fillBlueFlameCoordinates(coordinates, height, envelope, reducedMotion ? 1.4 : now * 0.0018);
        ctx.lineCap = "round";
        ctx.lineJoin = "round";
        ctx.globalCompositeOperation = "lighter";
        for (let strand = 0; strand < BLUE_FLAME_STRAND_COUNT; strand += 1) {
          const depth = strand / (BLUE_FLAME_STRAND_COUNT - 1);
          trace(strand);
          ctx.strokeStyle = blue;
          ctx.lineWidth = 2.4 + depth * 1.8;
          ctx.globalAlpha = envelope * (0.025 + depth * 0.014);
          ctx.stroke();
          ctx.strokeStyle = strand % 3 === 0 ? cyan : blue;
          ctx.lineWidth = (strand % 3 === 0 ? 1.1 : 0.75) / pixelRatio;
          ctx.globalAlpha = envelope * (0.25 + depth * 0.42);
          ctx.stroke();
        }
      }
      ctx.restore();
      const interval = reducedMotion ? (Math.abs(envelope - target) < 0.002 ? 250 : 100) : 1000 / 60;
      do {
        nextFrame += interval;
      } while (nextFrame <= now + 1.5);
    };
    resize();
    window.addEventListener("resize", resize);
    motion.addEventListener("change", changeMotion);
    frameId = requestAnimationFrame(draw);
    return () => {
      cancelAnimationFrame(frameId);
      window.removeEventListener("resize", resize);
      motion.removeEventListener("change", changeMotion);
    };
  }, [active, height, rmsRef, width]);

  return (
    <canvas
      ref={canvasRef}
      data-testid="native-recording-blue-flame"
      data-render-profile="typed-array-60hz-hidpi"
      aria-hidden="true"
      width={width}
      height={height}
      style={{
        position: "absolute",
        inset: 0,
        width,
        height,
        display: "block",
        background: BLUE_FLAME_PILL_BACKGROUND,
        borderRadius: height / 2,
        clipPath: `inset(0 round ${height / 2}px)`,
        pointerEvents: "none",
      }}
    />
  );
}
