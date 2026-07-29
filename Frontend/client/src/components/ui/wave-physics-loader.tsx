import { motion, useReducedMotion } from "motion/react";
import { useMemo } from "react";

import { useTheme } from "@/components/theme-provider";
import { cn } from "@/lib/utils";

export type WavePhysicsLoaderSize = "micro" | "inline" | "compact" | "panel";
export type WavePhysicsLoaderTheme = "auto" | "light" | "dark";

interface WavePhysicsLoaderProps {
  className?: string;
  label?: string;
  size?: WavePhysicsLoaderSize;
  theme?: WavePhysicsLoaderTheme;
}

interface LoaderMetrics {
  ballSize: number;
  barGap: number;
  barWidth: number;
  baseBarHeight: number;
  indent: number;
  maxBounce: number;
  numBars: number;
  stageHeight: number;
  wavePeakHeight: number;
}

interface LoaderFrames {
  bars: Array<{
    darkColors: string[];
    lightColors: string[];
    scales: number[];
  }>;
  ballScaleX: number[];
  ballScaleY: number[];
  ballX: number[];
  ballY: number[];
  times: number[];
}

const NUM_FRAMES = 201;
const BOUNCES = 4;
const DURATION_SECONDS = 4;

const METRICS: Record<WavePhysicsLoaderSize, LoaderMetrics> = {
  micro: {
    ballSize: 2.5,
    barGap: 1.1,
    barWidth: 1.5,
    baseBarHeight: 2.5,
    indent: 2.5,
    maxBounce: 7,
    numBars: 7,
    stageHeight: 13,
    wavePeakHeight: 6,
  },
  inline: {
    ballSize: 3,
    barGap: 1.4,
    barWidth: 2,
    baseBarHeight: 3,
    indent: 3,
    maxBounce: 8.5,
    numBars: 7,
    stageHeight: 16,
    wavePeakHeight: 7.5,
  },
  compact: {
    ballSize: 4,
    barGap: 1.8,
    barWidth: 2.5,
    baseBarHeight: 4,
    indent: 4.5,
    maxBounce: 12,
    numBars: 11,
    stageHeight: 25,
    wavePeakHeight: 11,
  },
  panel: {
    ballSize: 5,
    barGap: 2.25,
    barWidth: 3,
    baseBarHeight: 5,
    indent: 6,
    maxBounce: 17,
    numBars: 15,
    stageHeight: 34,
    wavePeakHeight: 15,
  },
};

const FRAME_CACHE = new Map<WavePhysicsLoaderSize, LoaderFrames>();

function interpolateChannel(start: number, end: number, amount: number): number {
  return Math.round(start + (end - start) * amount);
}

function colorAt(waveValue: number, dark: boolean): string {
  const start = dark ? [63, 63, 70] : [228, 228, 231];
  const end = dark ? [228, 228, 231] : [39, 39, 42];
  return `rgb(${interpolateChannel(start[0], end[0], waveValue)}, ${interpolateChannel(
    start[1],
    end[1],
    waveValue,
  )}, ${interpolateChannel(start[2], end[2], waveValue)})`;
}

function createFrames(size: WavePhysicsLoaderSize): LoaderFrames {
  const cached = FRAME_CACHE.get(size);
  if (cached) return cached;

  const metrics = METRICS[size];
  const barTravel = metrics.barWidth + metrics.barGap;
  const maxBarHeight = metrics.baseBarHeight + metrics.wavePeakHeight;
  const bars = Array.from({ length: metrics.numBars }, () => ({
    darkColors: [] as string[],
    lightColors: [] as string[],
    scales: [] as number[],
  }));
  const ballX: number[] = [];
  const ballY: number[] = [];
  const ballScaleX: number[] = [];
  const ballScaleY: number[] = [];
  const times: number[] = [];

  for (let frame = 0; frame < NUM_FRAMES; frame += 1) {
    const time = frame / (NUM_FRAMES - 1);
    const travelFraction = time < 0.5 ? time / 0.5 : (1 - time) / 0.5;
    const ballIndex = travelFraction * (metrics.numBars - 1);
    let bounceFraction = (travelFraction * BOUNCES) % 1;
    if (travelFraction === 0 || travelFraction === 1) bounceFraction = 0;

    const bounceHeight = 4 * bounceFraction * (1 - bounceFraction);
    const contactFactor = Math.max(0, 1 - bounceHeight * 2);
    const ballIndent = contactFactor * metrics.indent;
    const ballHeight = metrics.baseBarHeight + metrics.wavePeakHeight - ballIndent + bounceHeight * metrics.maxBounce;

    times.push(time);
    ballX.push(ballIndex * barTravel);
    ballY.push(-ballHeight);
    ballScaleY.push(1 - contactFactor * 0.3);
    ballScaleX.push(1 + contactFactor * 0.25);

    bars.forEach((bar, index) => {
      const distance = Math.abs(index - ballIndex);
      const waveValue = distance < 3 ? Math.cos((distance / 3) * (Math.PI / 2)) : 0;
      const indentValue =
        distance < 1.5 ? Math.cos((distance / 1.5) * (Math.PI / 2)) * contactFactor * metrics.indent : 0;
      const barHeight = Math.max(
        metrics.barWidth,
        metrics.baseBarHeight + waveValue * metrics.wavePeakHeight - indentValue,
      );

      bar.scales.push(barHeight / maxBarHeight);
      bar.lightColors.push(colorAt(waveValue, false));
      bar.darkColors.push(colorAt(waveValue, true));
    });
  }

  const frames = { bars, ballScaleX, ballScaleY, ballX, ballY, times };
  FRAME_CACHE.set(size, frames);
  return frames;
}

function paletteClasses(theme: WavePhysicsLoaderTheme): {
  ball: string;
  bar: string;
} {
  if (theme === "dark") {
    return { ball: "bg-zinc-100", bar: "bg-zinc-700" };
  }
  if (theme === "light") {
    return { ball: "bg-zinc-900", bar: "bg-zinc-200" };
  }
  return {
    ball: "bg-zinc-900 dark:bg-zinc-100",
    bar: "bg-zinc-200 dark:bg-zinc-700",
  };
}

export function WavePhysicsLoader({ className, label, size = "compact", theme = "auto" }: WavePhysicsLoaderProps) {
  const shouldReduceMotion = useReducedMotion();
  const { resolvedTheme } = useTheme();
  const metrics = METRICS[size];
  const frames = useMemo(() => createFrames(size), [size]);
  const palette = paletteClasses(theme);
  const stageWidth = metrics.numBars * metrics.barWidth + (metrics.numBars - 1) * metrics.barGap;
  const darkTheme = theme === "auto" ? resolvedTheme === "dark" : theme === "dark";

  return (
    <span
      className={cn("wave-physics-loader inline-flex shrink-0 items-center justify-center", className)}
      role={label ? "status" : undefined}
      aria-label={label}
      aria-hidden={label ? undefined : true}
      data-size={size}
      data-testid="wave-physics-loader"
    >
      <span
        className="relative flex items-end justify-start"
        style={{
          columnGap: metrics.barGap,
          height: metrics.stageHeight,
          width: stageWidth,
        }}
      >
        {frames.bars.map((bar, index) => (
          <motion.span
            key={index}
            className={cn("origin-bottom rounded-full", palette.bar)}
            style={{
              height: metrics.baseBarHeight + metrics.wavePeakHeight,
              width: metrics.barWidth,
            }}
            animate={
              shouldReduceMotion
                ? { scaleY: 0.42 }
                : {
                    scaleY: bar.scales,
                    backgroundColor: darkTheme ? bar.darkColors : bar.lightColors,
                  }
            }
            transition={
              shouldReduceMotion
                ? { duration: 0 }
                : {
                    duration: DURATION_SECONDS,
                    ease: "linear",
                    repeat: Infinity,
                    times: frames.times,
                  }
            }
          />
        ))}

        <motion.span
          className={cn("absolute bottom-0 left-0 rounded-full shadow-sm", palette.ball)}
          style={{
            height: metrics.ballSize,
            transformOrigin: "bottom center",
            width: metrics.ballSize,
          }}
          animate={
            shouldReduceMotion
              ? {
                  x: (stageWidth - metrics.ballSize) / 2,
                  y: -(metrics.baseBarHeight + metrics.wavePeakHeight),
                }
              : {
                  scaleX: frames.ballScaleX,
                  scaleY: frames.ballScaleY,
                  x: frames.ballX,
                  y: frames.ballY,
                }
          }
          transition={
            shouldReduceMotion
              ? { duration: 0 }
              : {
                  duration: DURATION_SECONDS,
                  ease: "linear",
                  repeat: Infinity,
                  times: frames.times,
                }
          }
        />
      </span>
    </span>
  );
}
