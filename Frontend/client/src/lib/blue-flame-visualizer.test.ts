import assert from "node:assert/strict";
import test from "node:test";

import { BLUE_FLAME_SAMPLE_COUNT, BLUE_FLAME_STRAND_COUNT, fillBlueFlameCoordinates } from "./blue-flame-visualizer";

test("blue flame geometry stays finite and inside the pill at every audio level", () => {
  const points = new Float32Array(BLUE_FLAME_SAMPLE_COUNT * BLUE_FLAME_STRAND_COUNT);
  for (const level of [-1, 0, 0.15, 0.7, 1, 5, Number.NaN]) {
    for (const phase of [0, 1.2, 100, Number.NaN]) {
      fillBlueFlameCoordinates(points, 41, level, phase);
      assert.ok(points.every((y) => Number.isFinite(y) && y >= 1 && y <= 40));
    }
  }
});

test("blue flame collapses at silence and changes its asymmetric filaments with audio and time", () => {
  const points = new Float32Array(BLUE_FLAME_SAMPLE_COUNT * BLUE_FLAME_STRAND_COUNT);
  fillBlueFlameCoordinates(points, 41, 0, 1);
  assert.ok(points.every((y) => y === 20.5));
  fillBlueFlameCoordinates(points, 41, 0.3, 1);
  const quietSpread = Math.max(...Array.from(points)) - Math.min(...Array.from(points));
  fillBlueFlameCoordinates(points, 41, 1, 1);
  assert.ok(Math.max(...Array.from(points)) - Math.min(...Array.from(points)) > quietSpread * 2);
  const earlier = points.slice();
  fillBlueFlameCoordinates(points, 41, 1, 2);
  assert.notDeepEqual(points, earlier);
  assert.notEqual(points[0], points[BLUE_FLAME_SAMPLE_COUNT - 1]);
  assert.notDeepEqual(
    points.subarray(0, BLUE_FLAME_SAMPLE_COUNT),
    points.subarray(BLUE_FLAME_SAMPLE_COUNT, BLUE_FLAME_SAMPLE_COUNT * 2),
  );
});

test("blue flame rejects an undersized reusable buffer", () => {
  assert.throws(() => fillBlueFlameCoordinates(new Float32Array(1), 41, 1, 1), RangeError);
});
