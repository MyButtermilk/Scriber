/** Fixed-budget eligibility rules for the combined pipeline study. */
export const ROUNDS = 5;
export const STAGES = Object.freeze(["check", "lint", "test", "build"]);

export function commandOutcome(record) {
  if (record.error != null || record.signal != null || record.timedOut || !Number.isInteger(record.status) || record.status < 0) return "fatal";
  return record.status === 0 ? "passed" : "command-exit";
}

export function plannedSamples(scenarios) {
  const planned = [];
  for (let round = 0; round < ROUNDS; round++) for (const scenario of scenarios) {
    const names = round % 2 ? ["candidate", "baseline"] : ["baseline", "candidate"];
    for (const [position, name] of names.entries()) planned.push({ name, scenario, round, position });
  }
  return planned;
}

export function collectionComplete(samples, plan) {
  return samples.length === plan.length && samples.every((sample, index) =>
    ["name", "scenario", "round", "position"].every(key => sample[key] === plan[index][key]));
}

export function compareBuildPair(pair) {
  if (pair.length !== 2 || new Set(pair.map(sample => sample.name)).size !== 2
    || pair[0].scenario !== pair[1].scenario || pair[0].round !== pair[1].round) throw new Error("Invalid baseline/candidate build pair");
  if (pair.some(sample => !sample.passed)) return { comparable: false, matches: null, reason: "At least one pipeline sample failed; no build comparison" };
  if (pair.some(sample => typeof sample.build?.sha256 !== "string" || !sample.build.sha256)) throw new Error("A successful pipeline sample omitted its build manifest hash");
  if (pair[0].build.sha256 !== pair[1].build.sha256) throw new Error("Baseline and candidate Node build manifests are not byte-identical");
  return { comparable: true, matches: true, reason: null };
}

export function summarizeGroup(samples, structurallyValid) {
  const rounds = samples.map(sample => sample.round).sort((a, b) => a - b);
  const complete = JSON.stringify(rounds) === JSON.stringify(Array.from({ length: ROUNDS }, (_, index) => index));
  const eligible = structurallyValid && complete && samples.every(sample => sample.passed
    && Number.isFinite(sample.seconds) && sample.seconds >= 0 && sample.stages.length === STAGES.length
    && sample.stages.every((stage, index) => stage.script === STAGES[index] && stage.passed && commandOutcome(stage) === "passed"
      && Number.isFinite(stage.seconds) && stage.seconds >= 0));
  const summarize = stage => {
    const values = samples.map(sample => stage ? sample.stages.find(value => value.script === stage) : sample).filter(value => value !== undefined);
    // An early successful stage from a failed pipeline is not an eligible
    // performance sample. Never calculate timings from a successful subset.
    const sorted = eligible ? values.map(value => value.seconds).sort((a, b) => a - b) : [];
    const passed = values.filter(value => value.passed).length;
    return {
      expected: ROUNDS, attempts: values.length, passed, failed: values.length - passed,
      notRun: stage ? samples.length - values.length : 0, notCollected: Math.max(0, ROUNDS - samples.length), eligible,
      medianSeconds: eligible ? sorted[2] : null, minSeconds: eligible ? sorted[0] : null, maxSeconds: eligible ? sorted.at(-1) : null,
    };
  };
  return { total: summarize(null), stages: Object.fromEntries(STAGES.map(stage => [stage, summarize(stage)])) };
}

export function compareGroups(baseline, candidate) {
  const comparable = baseline.total.eligible && candidate.total.eligible
    && baseline.total.medianSeconds > 0 && candidate.total.medianSeconds > 0;
  const baselineSeconds = comparable ? baseline.total.medianSeconds : null;
  const candidateSeconds = comparable ? candidate.total.medianSeconds : null;
  return {
    comparable, reason: comparable ? null : "Both candidate/scenario groups must contain all five valid complete pipelines",
    baselineSeconds, candidateSeconds, candidateOverBaseline: comparable ? candidateSeconds / baselineSeconds : null,
    savedPercent: comparable ? 100 * (1 - candidateSeconds / baselineSeconds) : null,
  };
}
