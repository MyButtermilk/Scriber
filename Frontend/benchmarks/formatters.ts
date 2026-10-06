import assert from "node:assert/strict";
import { performance } from "node:perf_hooks";
import { dateFormatter, numberFormatter } from "../client/src/i18n/formatters";

const count = 5_000;
const date = new Date("2026-10-05T12:34:56Z");
const options = { dateStyle: "medium", timeStyle: "short", timeZone: "UTC" } as const;
function run(cached: boolean): number {
  let checksum = 0;
  for (let i = 0; i < count; i++) {
    const locale = i % 2 ? "en-US" : "de-DE";
    checksum += (cached ? dateFormatter(locale, { ...options }) : new Intl.DateTimeFormat(locale, options)).format(
      date,
    ).length;
    checksum += (
      cached
        ? numberFormatter(locale, { maximumFractionDigits: 2 })
        : new Intl.NumberFormat(locale, { maximumFractionDigits: 2 })
    ).format(i / 3).length;
  }
  return checksum;
}
assert.equal(run(false), run(true));
function median(cached: boolean) {
  return Array.from({ length: 5 }, () => {
    const start = performance.now();
    run(cached);
    return performance.now() - start;
  }).sort((a, b) => a - b)[2];
}
console.log(
  JSON.stringify({ node: process.version, rows: count, beforeMs: median(false), afterMs: median(true) }, null, 2),
);
