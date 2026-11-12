/**
 * Alerts on indicators (docs/designs/indicator-alerts.md), as data with no DOM: which outputs
 * a condition can use, what the form must be before anything is sent, the words for an
 * alert's state, its warnings and its late fires, the legend bell's count, and the server's
 * errors in plain language. The rules mirror the server's (`alerts/indicator_rules.py`), which
 * stays the judge: this only saves a round trip and says it kindly.
 */

import { ApiError, type Alert, type AlertDirection, type AlertEvent, type IndicatorAlertCondition, type IndicatorAlertIn, type IndicatorAlertSpec, type IndicatorDef, type IndicatorOutput } from "../api";
import { kindOf } from "../chart/indicatorOutputs";

export type Condition = IndicatorAlertCondition;

export interface OutputChoice {
  name: string;
  label: string;
  kind: string;
}

const NUMBER_KINDS = ["line", "histogram", "area"];
const SIGNAL_KINDS = ["marker", "background"];

const choice = (o: IndicatorOutput): OutputChoice => ({ name: o.name, label: o.label || o.name, kind: kindOf(o) });
const ofKinds = (def: IndicatorDef, kinds: string[]): OutputChoice[] => def.outputs.filter((o) => kinds.includes(kindOf(o))).map(choice);

/** Outputs that have numbers to cross: a line, histogram or area. */
export const crossableOutputs = (def: IndicatorDef): OutputChoice[] => ofKinds(def, NUMBER_KINDS);

/** The other side of an output crossing an output: numbers, or a level; never the output itself. */
export const otherOutputs = (def: IndicatorDef, output: string): OutputChoice[] =>
  ofKinds(def, [...NUMBER_KINDS, "level"]).filter((o) => o.name !== output);

/** Outputs that are on or off for a bar: a marker or a background. */
export const signalOutputs = (def: IndicatorDef): OutputChoice[] => ofKinds(def, SIGNAL_KINDS);

export interface ConditionChoice {
  id: Condition;
  label: string;
  enabled: boolean;
  why?: string;
}

/** The three conditions, each enabled only when the indicator has outputs that fit it. */
export function availableConditions(def: IndicatorDef): ConditionChoice[] {
  const numbers = crossableOutputs(def);
  const pairs = ofKinds(def, [...NUMBER_KINDS, "level"]).length >= 2 && numbers.length >= 1;
  const signals = signalOutputs(def).length > 0;
  return [
    {
      id: "cross_value",
      label: "Output crosses a number",
      enabled: numbers.length > 0,
      why: numbers.length > 0 ? undefined : `${def.name} has no line, histogram or area to cross a number.`,
    },
    {
      id: "cross_output",
      label: "Output crosses another output",
      enabled: pairs,
      why: pairs ? undefined : `${def.name} has no second output to cross.`,
    },
    {
      id: "signal",
      label: "Marker or background fires",
      enabled: signals,
      why: signals ? undefined : `${def.name} has no marker or background.`,
    },
  ];
}

export interface IndicatorForm {
  output: string;
  condition: Condition | null;
  direction: AlertDirection;
  /** As typed. */
  value: string;
  otherOutput: string;
}

/** A finite number as typed: a point for decimals, an exponent allowed, nothing else. */
export function parseNumber(text: string): number | null {
  const t = text.trim();
  if (!/^[+-]?(\d+\.?\d*|\.\d+)(e[+-]?\d+)?$/i.test(t)) return null;
  const n = Number(t);
  return Number.isFinite(n) ? n : null;
}

/** The first thing wrong with the form, in words, or null when it can be sent. */
export function validateForm(form: IndicatorForm, def: IndicatorDef): string | null {
  if (form.condition === null) return "Choose a condition.";
  if (!form.output) return "Choose an output.";
  const output = def.outputs.find((o) => o.name === form.output);
  if (!output) return `${def.name} has no output "${form.output}".`;
  const kind = kindOf(output);
  if (form.condition === "signal") {
    return SIGNAL_KINDS.includes(kind) ? null : `"${form.output}" is a ${kind}; a signal needs a marker or a background.`;
  }
  if (!NUMBER_KINDS.includes(kind)) return `"${form.output}" is a ${kind}; a crossing needs a line, histogram or area.`;
  if (form.condition === "cross_value") {
    return parseNumber(form.value) === null ? "Enter a finite number to cross, with a point for decimals." : null;
  }
  if (!form.otherOutput) return "Choose a second output to cross.";
  if (form.otherOutput === form.output) return "The second output must be a different one.";
  const other = def.outputs.find((o) => o.name === form.otherOutput);
  if (!other) return `${def.name} has no second output "${form.otherOutput}".`;
  if (![...NUMBER_KINDS, "level"].includes(kindOf(other))) {
    return `"${form.otherOutput}" is a ${kindOf(other)}; the second output needs a line, histogram, area or level.`;
  }
  return null;
}

/** What goes to the server for a form that passed `validateForm`. */
export function buildIndicatorInput(uid: string, form: IndicatorForm): { direction: AlertDirection; indicator: IndicatorAlertIn } {
  const condition = form.condition as Condition;
  const indicator: IndicatorAlertIn = { uid, output: form.output, condition };
  if (condition === "cross_value") indicator.value = parseNumber(form.value) as number;
  if (condition === "cross_output") indicator.other_output = form.otherOutput;
  return { direction: condition === "signal" ? "cross" : form.direction, indicator };
}

const DIRECTION_WORDS: Record<AlertDirection, string> = { cross: "crossing", up: "crossing up", down: "crossing down" };

/** "RSI(14) rsi crossing up 30". */
export function conditionText(alert: Alert): string {
  const ind = alert.indicator;
  if (!ind) return "indicator condition unreadable";
  if (ind.condition === "signal") return `${ind.name} ${ind.output} fires`;
  const target = ind.condition === "cross_value" ? String(ind.value) : String(ind.other_output);
  return `${ind.name} ${ind.output} ${DIRECTION_WORDS[alert.direction]} ${target}`;
}

export type StateKind = "armed" | "arming" | "stopped" | "error" | "triggered" | "expired";

export interface AlertView {
  kind: StateKind;
  label: string;
  reason: string | null;
  behind: boolean;
  warnings: string[];
  /** The same warnings as a short label and the long words, for a row. */
  chips: { label: string; text: string }[];
}

const WARNINGS: Record<string, string> = {
  lookahead: "This script can see later bars (lookahead). A replay after an outage may differ from what it showed live.",
  warmup_short: "The warmup the script declares is shorter than it needs, so early values may differ from the chart.",
  warmup_cut: "The warmup it needs was cut, so this alert and the chart may start from different bars and differ slightly.",
  unchecked: "The script's checks are not checked yet: lookahead and warmup are still unknown.",
};

export const warningText = (code: string): string => WARNINGS[code] ?? code;

const WARNING_LABELS: Record<string, string> = { lookahead: "lookahead", warmup_short: "short warmup", warmup_cut: "warmup cut", unchecked: "unchecked" };

/** What the row says about a warning in a word or two (the words are its tooltip). */
export const warningLabel = (code: string): string => WARNING_LABELS[code] ?? code;

/** The state an alert shows: armed, arming, stopped (with why), error (with why), triggered, expired. */
export function viewOf(alert: Alert): AlertView {
  const codes = alert.state?.warnings ?? [];
  const warnings = codes.map(warningText);
  const chips = codes.map((c) => ({ label: warningLabel(c), text: warningText(c) }));
  const behind = alert.state?.behind ?? false;
  const make = (kind: StateKind, label: string, reason: string | null): AlertView => ({ kind, label, reason, behind, warnings, chips });
  if (alert.status === "stopped") return make("stopped", "Stopped", alert.stop_reason || "stopped");
  if (alert.status === "triggered") return make("triggered", "Triggered", null);
  if (alert.status === "expired") return make("expired", "Expired", null);
  if (alert.state?.error) return make("error", "Error", alert.state.error);
  if (alert.state && !alert.state.armed) {
    return make("arming", "Arming", "Waiting for the next closed bar to record where the indicator is; it never fires on that bar.");
  }
  return make("armed", "Armed", null);
}

export function closedBarNote(timeframe: string): string {
  return `Evaluated on closed ${timeframe} bars only: a signal arrives after the bar closes, never during it.`;
}

const LATE: Record<string, string> = {
  wake: "after wake-up",
  reconnect: "after a feed outage",
  restart: "after a restart",
  worker: "after the indicator could not be computed",
};

export const lateText = (late: string): string => LATE[late] ?? `after ${late}`;

/** The indicator alerts of one instance on one chart (the legend row's bell). */
export function instanceAlerts(alerts: readonly Alert[], instrument: string, uid: string | undefined): Alert[] {
  if (!uid) return [];
  return alerts.filter((a) => a.type === "indicator" && a.instrument === instrument && a.indicator?.uid === uid);
}

export function bellTitle(alerts: readonly Pick<Alert, "status">[]): string {
  if (!alerts.length) return "Add alert on this indicator";
  const active = alerts.filter((a) => a.status === "active").length;
  const what = alerts.length === 1 ? "1 alert" : `${alerts.length} alerts`;
  const tail = active === alerts.length ? "" : active === 0 ? " (none active)" : ` (${active} active)`;
  return `${what} on this indicator${tail}`;
}

export interface EventView {
  /** The message as the server sent it (the one Telegram got). */
  text: string;
  late: string | null;
  correction: string | null;
  indicator: boolean;
}

export function eventView(e: AlertEvent): EventView {
  return {
    text: e.message,
    late: e.late ? lateText(e.late) : null,
    correction: e.correction ? "The broker's candle later contradicted this fire; the fire stands and was not undone." : null,
    indicator: e.bar !== undefined && e.bar !== null,
  };
}

/** A failed call, said so a person can act on it. */
export function problemText(err: unknown): string {
  if (err instanceof ApiError) {
    if (err.status === 409) return `This indicator cannot be read right now: ${err.message}. Try again in a moment.`;
    if (err.status === 404) return "This alert or its chart no longer exists. Close this and look at the Alerts panel.";
    return err.message;
  }
  return err instanceof Error ? err.message : String(err);
}

type Named = Pick<IndicatorAlertSpec, "name" | "output" | "condition" | "other_output" | "value">;

/** The message the server gives an indicator alert when none is given ("EUR_USD, 1h RSI rsi crossing up 30"). */
export function defaultName(instrument: string, timeframe: string, direction: AlertDirection, ind: Named): string {
  if (ind.condition === "signal") return `${instrument}, ${timeframe} ${ind.name} ${ind.output} signal`;
  const target = ind.condition === "cross_value" ? String(ind.value) : String(ind.other_output);
  return `${instrument}, ${timeframe} ${ind.name} ${ind.output} ${DIRECTION_WORDS[direction]} ${target}`;
}

/**
 * The `name` to send, or undefined to leave it to the server. The server never renames an alert
 * on an edit, so a message it made itself would keep saying the old condition: it is made again
 * from the new one. A message the person wrote stays.
 */
export function nameToSend(opts: {
  typed: string;
  original: Alert | undefined;
  instrument: string;
  timeframe: string;
  direction: AlertDirection;
  next: Named;
}): string | undefined {
  const typed = opts.typed.trim();
  const { original } = opts;
  if (!original) return typed || undefined;
  const made = original.indicator ? defaultName(original.instrument, original.timeframe, original.direction, original.indicator) : null;
  if (typed === "" || (typed === original.name && original.name === made)) {
    return defaultName(opts.instrument, opts.timeframe, opts.direction, opts.next);
  }
  return typed;
}
