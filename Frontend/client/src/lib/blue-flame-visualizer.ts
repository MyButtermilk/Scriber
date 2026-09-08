import { clampUnit } from "./native-overlay-visualizer";

export const BLUE_FLAME_STRAND_COUNT = 16;
export const BLUE_FLAME_SAMPLE_COUNT = 96;

/** Reuses one caller-owned buffer; the flame never allocates point objects per frame. */
export function fillBlueFlameCoordinates(target: Float32Array, height: number, energy: number, phase: number): void {
  if (target.length < BLUE_FLAME_STRAND_COUNT * BLUE_FLAME_SAMPLE_COUNT) {
    throw new RangeError("Blue flame coordinate buffer is too small.");
  }
  const level = clampUnit(energy);
  const safeHeight = Math.max(1, Number(height) || 1);
  const safePhase = Number.isFinite(phase) ? phase : 0;
  for (let strand = 0; strand < BLUE_FLAME_STRAND_COUNT; strand += 1) {
    const depth = strand / (BLUE_FLAME_STRAND_COUNT - 1);
    const offset = strand * BLUE_FLAME_SAMPLE_COUNT;
    for (let point = 0; point < BLUE_FLAME_SAMPLE_COUNT; point += 1) {
      const x = point / (BLUE_FLAME_SAMPLE_COUNT - 1);
      const drift = safePhase + depth * 1.65;
      const plume = Math.sin(x * 8.3 - drift) * 0.48 + Math.sin(x * 17.7 + drift * 0.64) * 0.24;
      const turbulence = Math.sin(x * 34.1 - drift * 1.23 + plume * 2.3) * 0.13;
      const filament = Math.sin(x * 57.9 + drift * 0.71 + depth * 3.4) * 0.05;
      const spread = (depth - 0.5) * (0.34 + Math.sin(x * 11.2 - drift * 0.4) * 0.14);
      // The two ends stay alive; a gently offset crest avoids a mirrored seam.
      const crest = 0.6 + 0.4 * Math.pow(Math.sin(Math.PI * (x * 0.85 + 0.08)), 2);
      const displacement = (plume + turbulence + filament + spread) * level * safeHeight * 0.43 * crest;
      target[offset + point] = Math.min(safeHeight - 1, Math.max(1, safeHeight / 2 + displacement));
    }
  }
}
