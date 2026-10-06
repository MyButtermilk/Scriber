import assert from "node:assert/strict";
import test from "node:test";
import { dateFormatter, numberFormatter, resetDateFormatters } from "./formatters";

test("repeated formatting constructs one instance per locale and option set", () => {
  const native = Intl.NumberFormat;
  let constructions = 0;
  Intl.NumberFormat = new Proxy(native, {
    construct(target, args) {
      constructions++;
      return Reflect.construct(target, args);
    },
  });
  try {
    for (const locale of ["en-US", "de-DE"]) {
      const expected = new native(locale, { minimumFractionDigits: 2, maximumFractionDigits: 3 }).format(1234.567);
      for (let i = 0; i < 1_000; i++) {
        // Recreated option objects and property order should still hit the cache.
        const options =
          i % 2
            ? { minimumFractionDigits: 2, maximumFractionDigits: 3 }
            : { maximumFractionDigits: 3, minimumFractionDigits: 2 };
        assert.equal(numberFormatter(locale, options).format(1234.567), expected);
      }
    }
    assert.equal(constructions, 2);
  } finally {
    Intl.NumberFormat = native;
  }
});

test("dates preserve explicit time zones and can re-resolve the default after focus", () => {
  const value = new Date("2026-10-05T23:30:00Z");
  for (const locale of ["en-US", "de-DE"]) {
    for (const timeZone of ["UTC", "Europe/Berlin"]) {
      const options = { dateStyle: "medium", timeStyle: "short", timeZone } as const;
      assert.equal(
        dateFormatter(locale, options).format(value),
        new Intl.DateTimeFormat(locale, options).format(value),
      );
      assert.equal(dateFormatter(locale, { ...options }), dateFormatter(locale, options));
    }
  }
  const before = dateFormatter("en-US");
  resetDateFormatters();
  assert.notEqual(dateFormatter("en-US"), before);
});

test("formatter storage is bounded and invalid options retain native errors", () => {
  const first = numberFormatter("en-GB", { minimumIntegerDigits: 1, maximumFractionDigits: 0 });
  for (let i = 0; i < 40; i++)
    numberFormatter("en-GB", { minimumIntegerDigits: (i % 20) + 1, maximumFractionDigits: Math.floor(i / 20) });
  assert.notEqual(numberFormatter("en-GB", { minimumIntegerDigits: 1, maximumFractionDigits: 0 }), first);
  assert.throws(() => numberFormatter("en-US", { maximumFractionDigits: NaN }), RangeError);
  assert.throws(() => numberFormatter("en-US", { minimumFractionDigits: 10, maximumFractionDigits: 2 }), RangeError);
});

test("inherited and getter options bypass caching without changing their behavior", () => {
  const inherited = Object.create({ maximumFractionDigits: 0 }) as Intl.NumberFormatOptions;
  assert.equal(numberFormatter("en-US", inherited).format(1.6), "2");
  let digits = 0;
  const options = {
    get maximumFractionDigits() {
      return digits;
    },
  };
  assert.equal(numberFormatter("en-US", options).format(1.6), "2");
  digits = 2;
  assert.equal(numberFormatter("en-US", options).format(1.6), "1.6");
});
