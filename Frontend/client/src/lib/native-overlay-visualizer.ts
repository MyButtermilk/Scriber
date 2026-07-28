export const OVERLAY_RMS_NOISE_FLOOR = 0.00003;
export const OVERLAY_RMS_DISPLAY_SCALE = 90;
export const ENERGY_WAVE_FOCUS = 0.46;
export const ENERGY_WAVE_STRAND_COUNT = 9;
export const ENERGY_WAVE_SAMPLE_COUNT = 96;

const TAU = Math.PI * 2;

export function clampUnit(value: unknown): number {
  const numeric = Number(value);
  if (!Number.isFinite(numeric)) return 0;
  return Math.min(1, Math.max(0, numeric));
}

export function overlayVisualizerLevelFromRms(rms: unknown): number {
  const level = clampUnit(rms);
  if (level <= OVERLAY_RMS_NOISE_FLOOR) return 0;
  return Math.min(
    1,
    Math.pow(
      (level - OVERLAY_RMS_NOISE_FLOOR) * OVERLAY_RMS_DISPLAY_SCALE,
      0.72,
    ),
  );
}

export function advanceVisualizerEnvelope(
  current: number,
  target: number,
  deltaMs: number,
): number {
  const safeCurrent = clampUnit(current);
  const safeTarget = clampUnit(target);
  const safeDeltaMs = Math.min(100, Math.max(0, Number(deltaMs) || 0));
  const timeConstantMs = safeTarget > safeCurrent ? 65 : 380;
  const blend = 1 - Math.exp(-safeDeltaMs / timeConstantMs);
  return clampUnit(safeCurrent + (safeTarget - safeCurrent) * blend);
}

export function advanceReducedMotionEnvelope(
  current: number,
  target: number,
  deltaMs: number,
): number {
  const safeCurrent = clampUnit(current);
  const safeTarget = clampUnit(target);
  const safeDeltaMs = Math.min(150, Math.max(0, Number(deltaMs) || 0));
  const timeConstantMs = safeTarget > safeCurrent ? 320 : 620;
  const blend = 1 - Math.exp(-safeDeltaMs / timeConstantMs);
  return clampUnit(safeCurrent + (safeTarget - safeCurrent) * blend);
}

function smoothstep(start: number, end: number, value: number): number {
  const normalized = clampUnit((value - start) / Math.max(0.0001, end - start));
  return normalized * normalized * (3 - 2 * normalized);
}

function energyWaveYAt(
  progress: number,
  height: number,
  energy: number,
  phase: number,
  strandOffset: number,
): number {
  const strandPhase = strandOffset * 0.78;
  let y = height / 2;

  if (progress <= ENERGY_WAVE_FOCUS) {
    const approach = progress / ENERGY_WAVE_FOCUS;
    const entrance = smoothstep(0, 0.1, approach);
    const convergence = 1 - smoothstep(0.7, 1, approach);
    const carrier = Math.sin(
      approach * TAU * (2.15 + energy * 0.55)
        - phase
        + strandPhase * (1 - approach * 0.65),
    );
    const amplitude = (0.7 + energy * height * 0.17) * entrance * convergence;
    const helixSpread = strandOffset
      * Math.pow(Math.sin(Math.PI * approach), 1.25)
      * energy
      * height
      * 0.045;
    y += carrier * amplitude + helixSpread;
  } else {
    const release = (progress - ENERGY_WAVE_FOCUS) / (1 - ENERGY_WAVE_FOCUS);
    const spread = Math.pow(Math.max(0, Math.sin(Math.PI * release)), 1.3);
    const fan = strandOffset * spread * (0.8 + energy * height * 0.34);
    const ripple = Math.sin(
      release * TAU * (1.15 + energy * 0.3)
        - phase * 0.55
        + strandPhase,
    ) * spread * (0.28 + energy * 0.9);
    y += fan + ripple;
  }

  return y;
}

/**
 * Writes one strand into a caller-owned buffer. The animation hot path can
 * reuse a single Float32Array and therefore creates no point objects per frame.
 */
export function fillEnergyWaveYCoordinates(
  target: Float32Array,
  targetOffset: number,
  height: number,
  sampleCount: number,
  energy: number,
  phase: number,
  strandIndex: number,
  strandCount = ENERGY_WAVE_STRAND_COUNT,
): void {
  const safeHeight = Math.max(1, Number(height) || 1);
  const safeSamples = Math.max(2, Math.round(Number(sampleCount) || 2));
  const safeOffset = Math.max(0, Math.round(Number(targetOffset) || 0));
  if (safeOffset + safeSamples > target.length) {
    throw new RangeError("Energy wave buffer is smaller than the requested strand.");
  }

  const safeEnergy = clampUnit(energy);
  const safePhase = Number.isFinite(phase) ? phase : 0;
  const safeStrands = Math.max(1, Math.round(Number(strandCount) || 1));
  const centerStrand = (safeStrands - 1) / 2;
  const clampedStrandIndex = Math.min(
    safeStrands - 1,
    Math.max(0, Number(strandIndex) || 0),
  );
  const strandOffset = centerStrand > 0
    ? (clampedStrandIndex - centerStrand) / centerStrand
    : 0;

  for (let index = 0; index < safeSamples; index += 1) {
    const progress = index / (safeSamples - 1);
    target[safeOffset + index] = energyWaveYAt(
      progress,
      safeHeight,
      safeEnergy,
      safePhase,
      strandOffset,
    );
  }
}

export function energyWaveStrandVisibility(
  strandIndex: number,
  energy: number,
  strandCount = ENERGY_WAVE_STRAND_COUNT,
): number {
  const safeStrands = Math.max(1, Math.round(Number(strandCount) || 1));
  const center = (safeStrands - 1) / 2;
  const distance = center > 0
    ? Math.abs(Math.min(safeStrands - 1, Math.max(0, strandIndex)) - center) / center
    : 0;
  if (distance < 0.21) return 1;
  const activation = Math.max(0, distance * 0.62 - 0.08);
  return 0.035 + clampUnit((clampUnit(energy) - activation) / 0.22) * 0.965;
}
