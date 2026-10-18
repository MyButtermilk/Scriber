import baseline from "./eslint.config.js";
import { readFileSync } from "node:fs";

// Experimental: keep original implementations and path scopes for every rule
// whose behavior has not been matched by the native Oxlint candidate.
const nativeRules = new Set(
  JSON.parse(readFileSync(new URL("../tmp/speed-test-lint/native-eslint-rule-ids.json", import.meta.url))),
);
export default baseline.map((configuration) =>
  configuration.rules
    ? {
        ...configuration,
        rules: Object.fromEntries(
          Object.entries(configuration.rules).filter(([name]) => !nativeRules.has(name)),
        ),
      }
    : configuration,
);
