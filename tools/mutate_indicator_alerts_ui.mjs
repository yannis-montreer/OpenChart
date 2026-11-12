// Mutation check of the pure logic of the indicator alerts screens (IA-4,
// docs/decisions-indicator-alerts-ui.md): each mutant breaks one rule in a source file, the
// frontend tests must fail on it, and the file is put back whatever happens.
//
//   node tools/mutate_indicator_alerts_ui.mjs                # every mutant
//   node tools/mutate_indicator_alerts_ui.mjs "bell"         # the ones whose name contains it
//
// Exit code 0 when every mutant is killed, 1 when one survives, 2 when a mutant no longer
// applies (its text is not in the file exactly once) or the tests fail on the unbroken code.
// Run it from a clean tree: it rewrites files in place and restores them from memory.

import { spawnSync } from "node:child_process";
import { readFileSync, writeFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const frontend = join(dirname(fileURLToPath(import.meta.url)), "..", "frontend");
const A = "src/alerts/indicatorAlerts.ts";
const C = "src/alerts/confirmations.ts";
const D = "src/ui/definitionDelete.ts";
const I = "src/instruments.ts";
const API = "src/api.ts";
const S = "src/chart/chartSync.ts";
const L = "src/ui/layoutMenu.ts";

const TESTS = [
  "src/alerts",
  "src/api.alerts.test.ts",
  "src/instruments.test.ts",
  "src/ui/definitionDelete.test.ts",
  "src/ui/deleteDefinition.test.ts",
  "src/ui/layoutMenu.test.ts",
  "src/ui/indicatorAlertDialog.test.ts",
  "src/ui/alertsPanel.test.ts",
  "src/ui/indicatorBell.test.ts",
  "src/ui/removeInstance.test.ts",
  "src/chart/chartSync.test.ts",
];

/** [name, file, old, new, equivalent?] */
const MUTANTS = [
  ["a crossing may use a level as its output", A, 'const NUMBER_KINDS = ["line", "histogram", "area"];', 'const NUMBER_KINDS = ["line", "histogram", "area", "level"];'],
  ["a signal may not use a background", A, 'const SIGNAL_KINDS = ["marker", "background"];', 'const SIGNAL_KINDS = ["marker"];'],
  ["the second output may be the first", A, ".filter((o) => o.name !== output);", ".filter(() => true);"],
  ["infinity passes as a number", A, "return Number.isFinite(n) ? n : null;", "return n;"],
  ["a number is not trimmed", A, "const t = text.trim();", "const t = text;"],
  ["a signal keeps the chosen direction", A, 'direction: condition === "signal" ? "cross" : form.direction', "direction: form.direction"],
  ["a number crossing sends no number", A, 'if (condition === "cross_value") indicator.value', 'if (condition === "never") indicator.value'],
  ["a second output equal to the first passes", A, "if (form.otherOutput === form.output) return", "if (false) return"],
  ["a stopped alert shows no reason", A, 'alert.stop_reason || "stopped"', '"stopped"'],
  ["a stopped alert is not stopped", A, 'if (alert.status === "stopped") return make', 'if (alert.status === "never") return make'],
  ["an error is not shown", A, "if (alert.state?.error) return make", "if (false) return make"],
  ["arming is armed", A, "if (alert.state && !alert.state.armed) {", "if (alert.state && alert.state.armed) {"],
  ["the bell says none active when all run", A, 'const tail = active === alerts.length ? "" :', 'const tail = active === alerts.length ? " (none active)" :'],
  ["the bell counts active as none", A, 'active === 0 ? " (none active)"', 'active === 1 ? " (none active)"'],
  ["the bell counts another instrument's alerts", A, "a.instrument === instrument &&", "true &&"],
  // Equivalent: a line or level alert has no `indicator`, so `a.indicator?.uid === uid` is false for it
  // anyway. The type test is a second guard, kept so the rule reads as written in the design.
  ["the bell counts line alerts", A, 'a.type === "indicator" && a.instrument', "a.instrument", true],
  ["the bell counts another instance", A, "a.indicator?.uid === uid", "true"],
  ["a worker outage is not named", A, 'worker: "after the indicator could not be computed"', 'worker: "after a restart"'],
  ["a 409 is not said as unreadable", A, "if (err.status === 409) return", "if (err.status === 408) return"],
  ["an event is always an indicator event", A, "e.bar !== undefined && e.bar !== null", "true"],
  ["the message is not the server's", A, "text: e.message,", 'text: "",'],
  ["crossing an output needs one output only", A, ".length >= 2 && numbers.length >= 1", ".length >= 1 && numbers.length >= 1"],
  ["the closed-bar note loses the timeframe", A, "Evaluated on closed ${timeframe} bars only", "Evaluated on closed bars only"],
  ["a layout that stops nothing is asked", C, "if (!stopped.length) return", "if (stopped.length > 5) return"],
  ["the layout confirmation names no alert", C, "    ids: stopped.map((a) => a.id),", "    ids: [],"],
  ["any 409 carries a preview", C, 'if (!(err instanceof ApiError) || err.status !== 409) return null;', "if (!(err instanceof ApiError)) return null;"],
  ["removing asks about stopped alerts too", C, 'const running = alerts.filter((a) => a.status === "active");', "const running = alerts.filter(() => true);"],
  ["a definition delete forgets an alert", C, "(usage.alerts ?? []).map((a) => a.id)", "(usage.alerts ?? []).slice(1).map((a) => a.id)"],
  ["an alert's status is not listed", C, 'a.status === "active" ? "" : `, ${a.status}`', '""'],
  ["every alert counts as running", C, '(usage.alerts ?? []).filter((a) => a.status === "active").length', "(usage.alerts ?? []).length"],
  ["the delete button does not name the alerts", D, "if (runningAlerts > 0) return", "if (runningAlerts > 99) return"],
  ["the usage forgets the alert ids", D, "        inUse: true,\n        usage,\n        alertIds: definitionAlertIds(usage),", "        inUse: true,\n        usage,\n        alertIds: [],"],
  ["the delete text hides the stopped alerts", D, 'const stops = running ?', 'const stops = false ?'],
  ["Delete data hides the indicator alerts", I, "drawingsAndAlerts(s.drawings, s.alerts, s.indicator_alerts)", "drawingsAndAlerts(s.drawings, s.alerts)"],
  ["Delete data says indicators for one", I, 'onIndicators === 1 ? "an indicator" : "indicators"', 'onIndicators === 1 ? "indicators" : "an indicator"'],
  ["a list in the query keeps only the last", API, "for (const item of v) url.searchParams.append(k, item);", "for (const item of v) url.searchParams.set(k, item);"],
  ["the layout apply forgets the confirmed alerts", S, "this.api.apply(layoutId, instrument, stopAlerts)", "this.api.apply(layoutId, instrument, [])"],
  ["a refused layout is applied anyway", L, "if (stop === null) return;", ""],
  ["a layout is applied with no question", L, "this.hooks.confirmApply ? await this.hooks.confirmApply(layout) : []", "[]"],
];

// A run that was killed leaves its mutant in the file: put every mutant back first.
// (`git diff` shows nothing for a file that is new on a branch, so this does not rely on it.)
for (const [name, file, from, to] of MUTANTS) {
  const path = join(frontend, file);
  const text = readFileSync(path, "utf8");
  if (to && !text.includes(from) && text.includes(to)) {
    writeFileSync(path, text.replace(to, from));
    console.log(`restored    ${name}`);
  }
}

const quiet = { cwd: frontend, encoding: "utf8", shell: true };
function run() {
  const r = spawnSync("npx", ["vitest", "run", ...TESTS], quiet);
  return r.status === 0;
}

const only = process.argv[2];
let survived = 0;
let broken = 0;
if (!run()) {
  console.log("The tests fail on the unbroken code: nothing to check.");
  process.exit(2);
}
for (const [name, file, from, to, equivalent] of MUTANTS) {
  if (only && !name.includes(only)) continue;
  const path = join(frontend, file);
  const original = readFileSync(path, "utf8");
  const parts = original.split(from);
  if (parts.length !== 2) {
    console.log(`NOT APPLIED  ${name}  (${parts.length - 1} matches in ${file})`);
    broken++;
    continue;
  }
  try {
    writeFileSync(path, parts.join(to));
    const passed = run();
    console.log(`${passed ? (equivalent ? "equivalent " : "SURVIVED   ") : "killed     "}  ${name}`);
    if (passed && !equivalent) survived++;
  } finally {
    writeFileSync(path, original);
  }
}
console.log(`\n${MUTANTS.length} mutants: ${survived} survived, ${broken} not applied.`);
process.exit(broken ? 2 : survived ? 1 : 0);
