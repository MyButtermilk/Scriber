import { writeFileSync } from "node:fs";

export default async function* inventoryReporter(events) {
  const tests = [];
  const diagnostics = [];
  for await (const event of events) {
    if ((event.type === "test:pass" || event.type === "test:fail") && event.data.details?.type !== "suite") {
      tests.push({
        name: event.data.name,
        file: event.data.file,
        status: event.type === "test:pass" ? "passed" : "failed",
        skipped: Boolean(event.data.skip || event.data.todo),
      });
    } else if (event.type === "test:diagnostic") {
      diagnostics.push(event.data.message);
    }
  }
  const json = JSON.stringify({ tests, diagnostics }) + "\n";
  if (process.env.SCRIBER_NODE_TEST_REPORT) writeFileSync(process.env.SCRIBER_NODE_TEST_REPORT, json);
  yield json;
}
