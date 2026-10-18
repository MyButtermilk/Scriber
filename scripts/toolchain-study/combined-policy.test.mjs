import assert from "node:assert/strict";
import test from "node:test";
import { ROUNDS, STAGES, commandOutcome, plannedSamples, collectionComplete, compareBuildPair, summarizeGroup, compareGroups } from "./combined-policy.mjs";

const stage = (script, status = 0, extra = {}) => ({ script, status, error: null, signal: null, timedOut: false, passed: status === 0, seconds: 1, ...extra });
const sample = (round, name = "baseline", scenario = "cold", seconds = round + 1) => ({
  name, scenario, round, position: round % 2 ? Number(name === "baseline") : Number(name === "candidate"),
  seconds, passed: true, stages: STAGES.map(script => stage(script)), build: { sha256: "identical-build" },
});
const five = (name = "baseline", scenario = "cold") => Array.from({ length: ROUNDS }, (_, round) => sample(round, name, scenario));

test("positive command exits are ordinary failures; process errors remain fatal even with status zero", () => {
  assert.equal(commandOutcome(stage("test")), "passed");
  for (const status of [1, 2, 17]) assert.equal(commandOutcome(stage("test", status)), "command-exit");
  for (const extra of [{ error: "EPERM" }, { error: "" }, { signal: "SIGTERM" }, { timedOut: true }, { status: null }, { status: -1 }]) {
    assert.equal(commandOutcome(stage("test", 0, extra)), "fatal");
  }
});

test("the plan contains exactly five rounds in the declared alternating order, with no replacements", () => {
  const plan = plannedSamples(["cold", "warm", "changed"]);
  assert.equal(plan.length, 30);
  assert.equal(new Set(plan.map(value => `${value.name}/${value.scenario}/${value.round}`)).size, 30);
  assert.deepEqual(plan.slice(0, 2).map(value => value.name), ["baseline", "candidate"]);
  assert.deepEqual(plan.slice(6, 8).map(value => value.name), ["candidate", "baseline"]);
  assert.equal(collectionComplete(plan, plan), true);
  assert.equal(collectionComplete(plan.slice(0, -1), plan), false);
  const duplicate = structuredClone(plan); duplicate[duplicate.length - 1] = duplicate[0];
  assert.equal(collectionComplete(duplicate, plan), false);
  assert.equal(collectionComplete([...plan, plan[0]], plan), false);
});

test("a 4/5 pipeline group has no total or stage statistics, even when its first stages passed five times", () => {
  const samples = five();
  Object.assign(samples[4], { passed: false, seconds: 0.001, stages: [stage("check"), stage("lint"), stage("test", 1)] });
  delete samples[4].build;
  const result = summarizeGroup(samples, true);
  assert.deepEqual([result.total.attempts, result.total.passed, result.total.failed], [5, 4, 1]);
  assert.equal(result.stages.check.passed, 5);
  assert.deepEqual([result.stages.build.attempts, result.stages.build.failed, result.stages.build.notRun], [4, 0, 1]);
  for (const summary of [result.total, ...Object.values(result.stages)]) {
    assert.equal(summary.eligible, false);
    assert.equal(summary.medianSeconds, null);
    assert.equal(summary.minSeconds, null);
    assert.equal(summary.maxSeconds, null);
  }
});

test("five unique successful rounds allow the exact median; other scenarios do not poison that group", () => {
  const valid = summarizeGroup(five(), true);
  assert.equal(valid.total.eligible, true);
  assert.equal(valid.total.medianSeconds, 3);
  assert.equal(valid.total.minSeconds, 1);
  assert.equal(valid.total.maxSeconds, 5);
  assert.equal(summarizeGroup(five().slice(0, 4), true).total.eligible, false);
  const duplicate = five(); duplicate[4].round = 0;
  assert.equal(summarizeGroup(duplicate, true).total.eligible, false);
  assert.equal(summarizeGroup(five(), false).total.eligible, false);
});

test("paired speedups require both complete eligible groups and never use only green samples", () => {
  const baseline = summarizeGroup(five(), true);
  const candidateSamples = five("candidate").map(value => ({ ...value, seconds: value.seconds / 2 }));
  const candidate = summarizeGroup(candidateSamples, true);
  assert.equal(compareGroups(baseline, candidate).savedPercent, 50);
  candidateSamples[1].passed = false;
  const failed = summarizeGroup(candidateSamples, true);
  for (const comparison of [compareGroups(baseline, failed), compareGroups(failed, baseline)]) {
    assert.equal(comparison.comparable, false);
    for (const key of ["baselineSeconds", "candidateSeconds", "candidateOverBaseline", "savedPercent"]) assert.equal(comparison[key], null);
  }
});

test("a missing build after ordinary failure is not dereferenced; missing successful builds and parity errors are fatal", () => {
  const baseline = sample(0), candidate = sample(0, "candidate");
  assert.deepEqual(compareBuildPair([baseline, candidate]), { comparable: true, matches: true, reason: null });
  const failed = { ...baseline, passed: false, build: undefined };
  assert.equal(compareBuildPair([failed, candidate]).comparable, false);
  assert.throws(() => compareBuildPair([{ ...baseline, build: undefined }, candidate]), /omitted its build manifest/);
  assert.throws(() => compareBuildPair([baseline, { ...candidate, build: { sha256: "different" } }]), /not byte-identical/);
});
