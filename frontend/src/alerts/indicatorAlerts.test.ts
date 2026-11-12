import { describe, expect, it } from "vitest";

import type { Alert, AlertEvent, IndicatorDef, IndicatorOutput } from "../api";
import { ApiError } from "../api";
import {
  availableConditions,
  bellTitle,
  buildIndicatorInput,
  closedBarNote,
  conditionText,
  defaultName,
  eventView,
  instanceAlerts,
  lateText,
  nameToSend,
  otherOutputs,
  problemText,
  signalOutputs,
  validateForm,
  viewOf,
  warningLabel,
  warningText,
  crossableOutputs,
  type IndicatorForm,
} from "./indicatorAlerts";

const out = (o: Partial<IndicatorOutput> & { name: string }) => ({ label: o.name, color: "c", width: "w", ...o }) as IndicatorOutput;
const def = (outputs: IndicatorOutput[]): IndicatorDef => ({ id: "rsi", name: "RSI", title: "RSI", overlay: false, params: [], outputs, builtin: true });

const RSI = def([out({ name: "rsi" }), out({ name: "level:1", kind: "level", value: 70, input: null, width: null, style: "dashed" } as never)]);
const MACD = def([
  out({ name: "macd" }),
  out({ name: "signal" }),
  out({ name: "hist", kind: "histogram", base: 0 } as never),
  out({ name: "fill:1", kind: "fill", between: ["macd", "signal"], opacity: 0.1 } as never),
  out({ name: "buy", kind: "marker", shape: "arrow_up", position: "below", text: "" } as never),
  out({ name: "background:1", kind: "background", opacity: 0.1 } as never),
]);

function alert(over: Partial<Alert> = {}): Alert {
  return {
    id: "a1",
    instrument: "EUR_USD",
    type: "indicator",
    drawing_id: null,
    level: null,
    direction: "up",
    trigger_price: "mid",
    trigger_mode: "candle_close",
    timeframe: "1h",
    frequency: "once",
    cooldown_s: 0,
    name: "EUR_USD, 1h RSI crossing up 30",
    expires_at: null,
    notify: ["telegram", "app"],
    status: "active",
    created_at: 1,
    updated_at: 1,
    last_fired_at: null,
    fire_count: 0,
    indicator: { uid: "u1", kind: "builtin", id: "rsi", params: { length: 14 }, digest: "builtin", output: "rsi", condition: "cross_value", other_output: null, value: 30, name: "RSI(14)" },
    stop_reason: null,
    state: { last_bar: 100, armed: true, error: null, error_since: null, warnings: [], behind: false },
    ...over,
  } as Alert;
}

describe("which outputs a condition can use (the server's rule, before sending)", () => {
  it("crossings take a line, histogram or area; the other side may also be a level", () => {
    expect(crossableOutputs(MACD).map((o) => o.name)).toEqual(["macd", "signal", "hist"]);
    expect(otherOutputs(RSI, "rsi").map((o) => o.name)).toEqual(["level:1"]);
    expect(otherOutputs(MACD, "macd").map((o) => o.name)).toEqual(["signal", "hist"]);
  });

  it("a signal takes a marker or a background, never a fill", () => {
    expect(signalOutputs(MACD).map((o) => o.name)).toEqual(["buy", "background:1"]);
  });

  it("offers only the conditions the indicator can do, each with why not", () => {
    const rsi = availableConditions(RSI);
    expect(rsi.map((c) => [c.id, c.enabled])).toEqual([["cross_value", true], ["cross_output", true], ["signal", false]]);
    expect(rsi[2].why).toMatch(/no marker or background/i);
    const only = availableConditions(def([out({ name: "rsi" })]));
    expect(only[1].enabled).toBe(false);
    expect(only[1].why).toMatch(/second output/i);
    expect(availableConditions(MACD).every((c) => c.enabled)).toBe(true);
  });
});

const form = (over: Partial<IndicatorForm> = {}): IndicatorForm => ({ output: "rsi", condition: "cross_value", direction: "up", value: "30", otherOutput: "", ...over });

describe("validating before sending", () => {
  it("accepts a number crossing and builds the API body", () => {
    expect(validateForm(form(), RSI)).toBeNull();
    expect(buildIndicatorInput("u1", form())).toEqual({ direction: "up", indicator: { uid: "u1", output: "rsi", condition: "cross_value", value: 30 } });
  });

  it("refuses an empty, non-numeric or infinite number, in words", () => {
    for (const bad of ["", "  ", "abc", "1e999", "Infinity", "NaN"]) {
      expect(validateForm(form({ value: bad }), RSI), bad).toMatch(/number/i);
    }
    expect(validateForm(form({ value: "-0.5" }), RSI)).toBeNull();
    // Spaces around a number are not a mistake: it is sent trimmed.
    expect(validateForm(form({ value: "  30 " }), RSI)).toBeNull();
    expect(buildIndicatorInput("u1", form({ value: " 30.5\t" })).indicator.value).toBe(30.5);
    expect(validateForm(form({ value: "3,5" }), RSI)).toMatch(/number/i);
  });

  it("needs an output the indicator has and that fits the condition", () => {
    expect(validateForm(form({ output: "" }), RSI)).toMatch(/output/i);
    expect(validateForm(form({ output: "nope" }), RSI)).toMatch(/no output/i);
    expect(validateForm(form({ output: "level:1" }), RSI)).toMatch(/line, histogram or area/i);
    expect(validateForm(form({ condition: "signal", output: "macd" }), MACD)).toMatch(/marker or a background/i);
  });

  it("crossing another output needs a different, fitting second output", () => {
    const f = form({ condition: "cross_output", output: "rsi", otherOutput: "level:1", value: "" });
    expect(validateForm(f, RSI)).toBeNull();
    expect(buildIndicatorInput("u1", f)).toEqual({ direction: "up", indicator: { uid: "u1", output: "rsi", condition: "cross_output", other_output: "level:1" } });
    expect(validateForm({ ...f, otherOutput: "" }, RSI)).toMatch(/second output/i);
    expect(validateForm({ ...f, otherOutput: "rsi" }, RSI)).toMatch(/different/i);
    expect(validateForm({ ...f, output: "macd", otherOutput: "buy" }, MACD)).toMatch(/second output/i);
  });

  it("a signal has no number, no second output and no direction", () => {
    const f = form({ condition: "signal", output: "buy", direction: "down", value: "5", otherOutput: "macd" });
    expect(validateForm(f, MACD)).toBeNull();
    expect(buildIndicatorInput("u1", f)).toEqual({ direction: "cross", indicator: { uid: "u1", output: "buy", condition: "signal" } });
  });

  it("needs a condition", () => {
    expect(validateForm(form({ condition: null }), RSI)).toMatch(/condition/i);
  });
});

describe("the condition in words", () => {
  it("says what crosses what, with the number as typed by the server", () => {
    expect(conditionText(alert())).toBe("RSI(14) rsi crossing up 30");
    expect(conditionText(alert({ direction: "cross" }))).toBe("RSI(14) rsi crossing 30");
    expect(conditionText(alert({ direction: "down", indicator: { ...alert().indicator!, condition: "cross_output", other_output: "level:1", value: null } }))).toBe("RSI(14) rsi crossing down level:1");
    expect(conditionText(alert({ direction: "cross", indicator: { ...alert().indicator!, condition: "signal", output: "buy", value: null } }))).toBe("RSI(14) buy fires");
  });

  it("copes with an incomplete condition", () => {
    expect(conditionText(alert({ indicator: null }))).toBe("indicator condition unreadable");
  });
});

describe("the state an alert shows", () => {
  it("armed, and arming before the first evaluation", () => {
    expect(viewOf(alert())).toMatchObject({ kind: "armed", label: "Armed", reason: null });
    expect(viewOf(alert({ state: { ...alert().state!, armed: false } }))).toMatchObject({ kind: "arming", label: "Arming" });
    expect(viewOf(alert({ state: { ...alert().state!, armed: false } })).reason).toMatch(/next closed bar/i);
  });

  it("stopped with the reason the server gave", () => {
    for (const why of ["stopped by you", "the indicator was removed from the chart", "output rsi no longer exists"]) {
      expect(viewOf(alert({ status: "stopped", stop_reason: why }))).toMatchObject({ kind: "stopped", reason: why });
    }
    expect(viewOf(alert({ status: "stopped", stop_reason: null })).reason).toBe("stopped");
  });

  it("an error names its reason, and wins over armed", () => {
    const v = viewOf(alert({ state: { ...alert().state!, error: "the indicator RSI no longer exists" } }));
    expect(v).toMatchObject({ kind: "error", label: "Error", reason: "the indicator RSI no longer exists" });
  });

  it("an error on a stopped alert is not shown: it is stopped", () => {
    expect(viewOf(alert({ status: "stopped", stop_reason: "stopped by you", state: { ...alert().state!, error: "x" } })).kind).toBe("stopped");
  });

  it("triggered and expired keep their words; behind is a flag", () => {
    expect(viewOf(alert({ status: "triggered" })).kind).toBe("triggered");
    expect(viewOf(alert({ status: "expired" })).kind).toBe("expired");
    expect(viewOf(alert({ state: { ...alert().state!, behind: true } })).behind).toBe(true);
  });

  it("a line alert has no indicator state: armed while active", () => {
    const line = { ...alert({ type: "line_cross" }), state: undefined, indicator: undefined } as unknown as Alert;
    expect(viewOf(line).kind).toBe("armed");
  });
});

describe("warnings are shown, never blocking", () => {
  it("each has words that say what it means", () => {
    expect(warningText("lookahead")).toMatch(/later bars/i);
    expect(warningText("lookahead")).toMatch(/replay/i);
    expect(warningText("warmup_short")).toMatch(/warmup/i);
    expect(warningText("warmup_cut")).toMatch(/chart/i);
    expect(warningText("unchecked")).toMatch(/not checked/i);
    expect(warningText("something_new")).toBe("something_new");
  });

  it("each has a short label for the row, and the long words for its tooltip", () => {
    expect(["lookahead", "warmup_short", "warmup_cut", "unchecked", "other"].map(warningLabel)).toEqual(["lookahead", "short warmup", "warmup cut", "unchecked", "other"]);
    const v = viewOf(alert({ state: { ...alert().state!, warnings: ["lookahead", "warmup_cut"] } }));
    expect(v.chips).toEqual([
      { label: "lookahead", text: warningText("lookahead") },
      { label: "warmup cut", text: warningText("warmup_cut") },
    ]);
  });

  it("viewOf carries them in order", () => {
    expect(viewOf(alert({ state: { ...alert().state!, warnings: ["lookahead", "warmup_cut"] } })).warnings).toEqual([warningText("lookahead"), warningText("warmup_cut")]);
  });
});

describe("closed bars", () => {
  it("says a signal arrives after the bar of the alert's timeframe closes", () => {
    expect(closedBarNote("1h")).toMatch(/closed 1h bar/);
    expect(closedBarNote("1h")).toMatch(/after the bar closes/i);
    expect(closedBarNote("1m")).toMatch(/closed 1m bar/);
  });
});

describe("late fires", () => {
  it("name their cause, including the indicator being unavailable", () => {
    expect(lateText("restart")).toBe("after a restart");
    expect(lateText("wake")).toBe("after wake-up");
    expect(lateText("reconnect")).toBe("after a feed outage");
    expect(lateText("worker")).toBe("after the indicator could not be computed");
  });
});

describe("the instance's alerts and its bell", () => {
  const list = [
    alert({ id: "a", status: "active" }),
    alert({ id: "b", status: "stopped", stop_reason: "stopped by you" }),
    alert({ id: "c", instrument: "XAU_USD" }),
    alert({ id: "d", indicator: { ...alert().indicator!, uid: "u2" } }),
    alert({ id: "e", type: "line_cross", indicator: undefined } as never),
  ];

  it("picks the indicator alerts of one uid on one instrument", () => {
    expect(instanceAlerts(list, "EUR_USD", "u1").map((a) => a.id)).toEqual(["a", "b"]);
    expect(instanceAlerts(list, "XAU_USD", "u1").map((a) => a.id)).toEqual(["c"]);
    expect(instanceAlerts(list, "EUR_USD", undefined)).toEqual([]);
  });

  it("the bell counts all of them and says how many run", () => {
    expect(bellTitle([])).toBe("Add alert on this indicator");
    expect(bellTitle([list[0]])).toBe("1 alert on this indicator");
    expect(bellTitle([list[0], list[1]])).toBe("2 alerts on this indicator (1 active)");
    expect(bellTitle([list[1]])).toBe("1 alert on this indicator (none active)");
  });
});

describe("the event log shows the server's message", () => {
  const ev = (over: Partial<AlertEvent> = {}): AlertEvent => ({
    id: 1, alert_id: "a1", ts: 1, price: 1.1, line_value: null, cross: "up", message: "EUR_USD 1h RSI(14) crossed up 30.00 at bar close 14:00",
    late: null, detected_at: 2, delivered: true, attempts: 1, error: null, bar: 1000, value: 31, other_value: null, correction: null, observed: 1, observed_prev: -1, frame_start: 1, ...over,
  });

  it("uses the message as sent, not a rebuilt one", () => {
    expect(eventView(ev()).text).toBe("EUR_USD 1h RSI(14) crossed up 30.00 at bar close 14:00");
  });

  it("marks a late fire and a correction", () => {
    expect(eventView(ev({ late: "worker" })).late).toBe("after the indicator could not be computed");
    expect(eventView(ev()).late).toBeNull();
    expect(eventView(ev({ correction: { value: 29 } })).correction).toMatch(/broker/i);
    expect(eventView(ev()).correction).toBeNull();
  });

  it("is an indicator event only when it has a bar", () => {
    expect(eventView(ev()).indicator).toBe(true);
    expect(eventView({ ...ev(), bar: undefined }).indicator).toBe(false);
  });
});

describe("server errors in words", () => {
  const e = (status: number, msg: string) => new ApiError(status, msg);
  it("409 says the indicator cannot be read now, and keeps the server's reason", () => {
    expect(problemText(e(409, "worker is not running"))).toMatch(/cannot be read right now.*worker is not running/i);
  });
  it("400 and 422 show the server's sentence", () => {
    expect(problemText(e(400, "no indicator 'u9' on the EUR_USD chart"))).toBe("no indicator 'u9' on the EUR_USD chart");
    expect(problemText(e(422, "Input should be a valid number"))).toBe("Input should be a valid number");
  });
  it("404 says the alert or chart is gone; other errors keep their text", () => {
    expect(problemText(e(404, "no such alert"))).toMatch(/no longer exists/i);
    expect(problemText(new Error("Failed to fetch"))).toBe("Failed to fetch");
  });
});

describe("the alert's message", () => {
  const spec = alert().indicator!;
  const next = { name: "RSI(14)", output: "rsi", condition: "cross_value" as const, other_output: null, value: 35 };

  it("is named from the condition, as the server names it", () => {
    expect(defaultName("EUR_USD", "1h", "down", { ...spec, name: "RSI" })).toBe("EUR_USD, 1h RSI rsi crossing down 30");
    expect(defaultName("EUR_USD", "1h", "cross", { ...spec, name: "RSI", value: 0.5 })).toBe("EUR_USD, 1h RSI rsi crossing 0.5");
    expect(defaultName("EUR_USD", "1h", "up", { ...spec, name: "MACD", output: "macd", condition: "cross_output", other_output: "signal", value: null })).toBe("EUR_USD, 1h MACD macd crossing up signal");
    expect(defaultName("EUR_USD", "1h", "cross", { ...spec, name: "MACD", output: "buy", condition: "signal", value: null })).toBe("EUR_USD, 1h MACD buy signal");
  });

  it("is left to the server when a new alert has no message", () => {
    expect(nameToSend({ typed: "", original: undefined, instrument: "EUR_USD", timeframe: "1h", direction: "up", next })).toBeUndefined();
    expect(nameToSend({ typed: "  my name ", original: undefined, instrument: "EUR_USD", timeframe: "1h", direction: "up", next })).toBe("my name");
  });

  it("follows the condition when the person never wrote one (an edit keeps what they wrote)", () => {
    const original = alert({ name: defaultName("EUR_USD", "1h", "up", spec) });
    const args = { original, instrument: "EUR_USD", timeframe: "1h", direction: "up" as const, next };
    expect(nameToSend({ ...args, typed: original.name })).toBe("EUR_USD, 1h RSI(14) rsi crossing up 35");
    expect(nameToSend({ ...args, typed: "" })).toBe("EUR_USD, 1h RSI(14) rsi crossing up 35");
    expect(nameToSend({ ...args, typed: "Gold watch" })).toBe("Gold watch");
    const written = alert({ name: "Gold watch" });
    expect(nameToSend({ ...args, original: written, typed: "Gold watch" })).toBe("Gold watch");
  });
});
