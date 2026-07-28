import assert from "node:assert/strict";
import test from "node:test";

import {
  ENERGY_WAVE_FOCUS,
  ENERGY_WAVE_SAMPLE_COUNT,
  ENERGY_WAVE_STRAND_COUNT,
  advanceReducedMotionEnvelope,
  advanceVisualizerEnvelope,
  energyWaveStrandVisibility,
  fillEnergyWaveYCoordinates,
  overlayVisualizerLevelFromRms,
} from "./native-overlay-visualizer";

test("maps microphone RMS into a bounded monotonic display level", () => {
  assert.equal(overlayVisualizerLevelFromRms(Number.NaN), 0);
  assert.equal(overlayVisualizerLevelFromRms(-1), 0);
  assert.equal(overlayVisualizerLevelFromRms(0.00003), 0);
  assert.equal(overlayVisualizerLevelFromRms(2), 1);

  const quiet = overlayVisualizerLevelFromRms(0.0005);
  const conversational = overlayVisualizerLevelFromRms(0.005);
  const loud = overlayVisualizerLevelFromRms(0.02);
  assert.ok(quiet > 0);
  assert.ok(quiet < conversational);
  assert.ok(conversational < loud);
  assert.ok(loud <= 1);
});

test("uses a faster attack than release while keeping the envelope bounded", () => {
  const attack = advanceVisualizerEnvelope(0, 1, 50);
  const release = advanceVisualizerEnvelope(1, 0, 50);
  assert.ok(attack > 0 && attack < 1);
  assert.ok(release > 0 && release < 1);
  assert.ok(attack > 1 - release);
  assert.equal(advanceVisualizerEnvelope(-1, 2, 1000), 1 - Math.exp(-100 / 65));
});

test("smooths reduced-motion amplitude without coarse threshold jumps", () => {
  const first = advanceReducedMotionEnvelope(0.5, 0.63, 100);
  const second = advanceReducedMotionEnvelope(first, 0.49, 100);
  assert.ok(first > 0.5 && first < 0.63);
  assert.ok(second < first && second > 0.49);
  assert.ok(first - 0.5 < 0.05);
  assert.equal(advanceReducedMotionEnvelope(-1, 2, 1000), 1 - Math.exp(-150 / 320));
});

test("fills deterministic converging wave geometry without point allocations", () => {
  const sampleCount = ENERGY_WAVE_SAMPLE_COUNT;
  const first = new Float32Array(sampleCount);
  const second = new Float32Array(sampleCount);

  fillEnergyWaveYCoordinates(first, 0, 29, sampleCount, 0.65, 1.25, 0);
  fillEnergyWaveYCoordinates(second, 0, 29, sampleCount, 0.65, 1.25, 0);

  assert.deepEqual(first, second);
  assert.ok(first.every(Number.isFinite));
  assert.ok(Math.abs(first.at(-1)! - 14.5) < 0.000001);
});

test("outer strands converge at the focal sample", () => {
  const sampleCount = 201;
  const values = new Float32Array(sampleCount * 2);
  fillEnergyWaveYCoordinates(values, 0, 30, sampleCount, 1, 0.5, 0);
  fillEnergyWaveYCoordinates(
    values,
    sampleCount,
    30,
    sampleCount,
    1,
    0.5,
    ENERGY_WAVE_STRAND_COUNT - 1,
  );

  const focusIndex = Math.round(ENERGY_WAVE_FOCUS * (sampleCount - 1));
  assert.ok(Math.abs(values[0] - 15) < 0.000001);
  assert.ok(Math.abs(values[sampleCount] - 15) < 0.000001);
  assert.ok(Math.abs(values[focusIndex] - 15) < 0.000001);
  assert.ok(Math.abs(values[sampleCount + focusIndex] - 15) < 0.000001);
});

test("rejects undersized caller buffers", () => {
  assert.throws(
    () => fillEnergyWaveYCoordinates(new Float32Array(4), 0, 29, 5, 0.5, 0, 0),
    RangeError,
  );
});

test("reveals outer wave modes as microphone energy rises", () => {
  const outer = 0;
  const center = Math.floor(ENERGY_WAVE_STRAND_COUNT / 2);
  assert.equal(energyWaveStrandVisibility(center, 0), 1);
  assert.ok(energyWaveStrandVisibility(outer, 0) < 0.1);
  assert.ok(energyWaveStrandVisibility(outer, 1) > 0.95);
});
