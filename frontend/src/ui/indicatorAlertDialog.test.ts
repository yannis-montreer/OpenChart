// @vitest-environment happy-dom
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { ApiError, alertsApi, type Alert, type IndicatorDef, type IndicatorOutput } from "../api";
import { openIndicatorAlertDialog } from "./indicatorAlertDialog";

const out = (o: object) => ({ label: "x", color: "c", width: "w", ...o }) as IndicatorOutput;
const DEF: IndicatorDef = {
  id: "macd",
  name: "MACD",
  title: "MACD",
  overlay: false,
  params: [],
  builtin: true,
  outputs: [out({ name: "macd" }), out({ name: "signal" }), out({ name: "buy", kind: "marker", shape: "arrow_up", position: "below", text: "" })],
};

function saved(over: Partial<Alert> = {}): Alert {
  return {
    id: "a1", instrument: "EUR_USD", type: "indicator", drawing_id: null, level: null, direction: "up", trigger_price: "mid", trigger_mode: "candle_close",
    timeframe: "1h", frequency: "once", cooldown_s: 0, name: "n", expires_at: null, notify: ["telegram", "app"], status: "active", created_at: 1, updated_at: 1,
    last_fired_at: null, fire_count: 0, stop_reason: null,
    indicator: { uid: "u1", kind: "builtin", id: "macd", params: {}, digest: "builtin", output: "macd", condition: "cross_value", other_output: null, value: 0, name: "MACD" },
    state: { last_bar: null, armed: false, error: null, error_since: null, warnings: [], behind: false },
    ...over,
  } as Alert;
}

const open = (over: Partial<Parameters<typeof openIndicatorAlertDialog>[0]> = {}) => {
  const onSaved = vi.fn();
  const onDeleted = vi.fn();
  openIndicatorAlertDialog({ instrument: "EUR_USD", timeframe: "1h", price: "mid", def: DEF, uid: "u1", title: "MACD(12, 26, 9)", telegramReady: true, onSaved, onDeleted, ...over });
  return { onSaved, onDeleted };
};
const q = <T extends Element>(sel: string) => document.querySelector<T>(sel);
const $ = <T extends Element>(sel: string) => {
  const el = q<T>(sel);
  if (!el) throw new Error(`no ${sel}`);
  return el;
};
const set = (sel: string, value: string) => {
  const el = $<HTMLInputElement | HTMLSelectElement>(sel);
  el.value = value;
  el.dispatchEvent(new Event(el.tagName === "SELECT" ? "change" : "input", { bubbles: true }));
};
const click = (text: string) => {
  const b = [...document.querySelectorAll("button")].find((x) => x.textContent?.trim() === text || x.getAttribute("aria-label") === text);
  if (!b) throw new Error(`no button ${text}`);
  b.click();
};
const tick = () => new Promise((r) => setTimeout(r, 0));

beforeEach(() => {
  document.body.replaceChildren();
});
afterEach(() => vi.restoreAllMocks());

describe("the creation dialog", () => {
  it("says which indicator and chart, and that signals arrive after the bar closes", () => {
    open();
    expect($(".al-chip").textContent).toBe("EUR_USD, 1h");
    expect(document.body.textContent).toContain("MACD(12, 26, 9)");
    expect($(".al-closed-note").textContent).toMatch(/closed 1h bar/);
    expect($(".al-closed-note").textContent).toMatch(/after the bar closes/i);
  });

  it("offers the outputs that fit the condition, the conditions and the direction", () => {
    open();
    const outputs = () => [...document.querySelectorAll("select[aria-label=Output] option")].map((o) => o.getAttribute("value"));
    expect(outputs()).toEqual(["macd", "signal"]);
    set("select[aria-label=Condition]", "signal");
    expect(outputs()).toEqual(["buy"]);
    set("select[aria-label=Condition]", "cross_output");
    expect(outputs()).toEqual(["macd", "signal"]);
    expect([...document.querySelectorAll("select[aria-label='Second output'] option")].map((o) => o.getAttribute("value"))).toEqual(["signal"]);
    expect([...document.querySelectorAll("select[aria-label=Condition] option")].map((o) => o.getAttribute("value"))).toEqual(["cross_value", "cross_output", "signal"]);
    expect([...document.querySelectorAll("select[aria-label=Direction] option")].map((o) => o.getAttribute("value"))).toEqual(["cross", "up", "down"]);
  });

  it("shows only the fields of the chosen condition", () => {
    open();
    expect($("[data-field=value]").hasAttribute("hidden")).toBe(false);
    expect($("[data-field=other]").hasAttribute("hidden")).toBe(true);
    set("select[aria-label=Condition]", "cross_output");
    expect($("[data-field=value]").hasAttribute("hidden")).toBe(true);
    expect($("[data-field=other]").hasAttribute("hidden")).toBe(false);
    set("select[aria-label=Condition]", "signal");
    expect($("[data-field=direction]").hasAttribute("hidden")).toBe(true);
    expect($("[data-field=value]").hasAttribute("hidden")).toBe(true);
  });

  it("creates the alert with exactly what the person chose", async () => {
    const create = vi.spyOn(alertsApi, "create").mockResolvedValue(saved());
    const { onSaved } = open();
    set("input[aria-label=Number]", "0.5");
    set("select[aria-label=Direction]", "down");
    click("Create");
    await tick();
    expect(create).toHaveBeenCalledTimes(1);
    expect(create.mock.calls[0][0]).toMatchObject({
      instrument: "EUR_USD", type: "indicator", timeframe: "1h", trigger_price: "mid", trigger_mode: "candle_close", direction: "down", frequency: "once",
      indicator: { uid: "u1", output: "macd", condition: "cross_value", value: 0.5 },
    });
    expect(onSaved).toHaveBeenCalledWith(saved());
    expect(q("[role=dialog]")).toBeNull();
  });

  it("validates before sending: nothing is sent, and the words say what to fix", async () => {
    const create = vi.spyOn(alertsApi, "create");
    open();
    set("input[aria-label=Number]", "abc");
    click("Create");
    await tick();
    expect(create).not.toHaveBeenCalled();
    expect($(".result").textContent).toMatch(/number/i);
    expect(q("[role=dialog]")).not.toBeNull();
  });

  it("a signal condition sends no number and no direction but cross", async () => {
    const create = vi.spyOn(alertsApi, "create").mockResolvedValue(saved());
    open();
    set("select[aria-label=Condition]", "signal");
    set("select[aria-label=Output]", "buy");
    click("Create");
    await tick();
    expect(create.mock.calls[0][0]).toMatchObject({ direction: "cross", indicator: { uid: "u1", output: "buy", condition: "signal" } });
    expect(create.mock.calls[0][0].indicator).not.toHaveProperty("value");
  });

  it("shows a server error in words and stays open to fix it", async () => {
    vi.spyOn(alertsApi, "create").mockRejectedValue(new ApiError(409, "the worker is not running"));
    open();
    set("input[aria-label=Number]", "1");
    click("Create");
    await tick();
    expect($(".result").textContent).toMatch(/cannot be read right now.*the worker is not running/i);
    expect(q("[role=dialog]")).not.toBeNull();
    expect($<HTMLButtonElement>(".al-foot .btn-light").disabled).toBe(false);
  });

  it("keeps the dialog open with the warnings the server returned, never blocking", async () => {
    vi.spyOn(alertsApi, "create").mockResolvedValue(saved({ state: { ...saved().state!, warnings: ["lookahead", "warmup_short"] } }));
    const { onSaved } = open();
    set("input[aria-label=Number]", "1");
    click("Create");
    await tick();
    expect(onSaved).toHaveBeenCalled();
    const warnings = [...document.querySelectorAll(".al-warnings li")].map((l) => l.textContent);
    expect(warnings).toHaveLength(2);
    expect(warnings[0]).toMatch(/later bars/i);
    expect(document.body.textContent).toMatch(/alert is created/i);
    click("Done");
    expect(q("[role=dialog]")).toBeNull();
  });
});

describe("editing", () => {
  it("fills the form from the alert, shows its warnings and stop reason, and saves with the same uid", async () => {
    const alert = saved({ status: "stopped", stop_reason: "stopped by you", state: { ...saved().state!, warnings: ["warmup_cut"] } });
    const update = vi.spyOn(alertsApi, "update").mockResolvedValue(saved());
    open({ alert });
    expect($<HTMLInputElement>("input[aria-label=Number]").value).toBe("0");
    expect($("select[aria-label=Output]")).toHaveProperty("value", "macd");
    expect($(".al-status-note").textContent).toMatch(/stopped by you/);
    expect([...document.querySelectorAll(".al-warnings li")]).toHaveLength(1);
    set("input[aria-label=Number]", "2");
    click("Save");
    await tick();
    expect(update).toHaveBeenCalledWith("a1", expect.objectContaining({ status: "active", indicator: { uid: "u1", output: "macd", condition: "cross_value", value: 2 } }));
  });

  it("a name the server made follows the new condition; one the person wrote stays", async () => {
    const update = vi.spyOn(alertsApi, "update").mockResolvedValue(saved());
    open({ alert: saved({ name: "EUR_USD, 1h MACD macd crossing up 0" }) });
    set("input[aria-label=Number]", "2");
    click("Save");
    await tick();
    expect(update.mock.calls[0][1].name).toBe("EUR_USD, 1h MACD macd crossing up 2");
    document.body.replaceChildren();
    open({ alert: saved({ name: "my own words" }) });
    set("input[aria-label=Number]", "3");
    click("Save");
    await tick();
    expect(update.mock.calls[1][1].name).toBe("my own words");
  });

  it("an old complaint goes away when the form changes", async () => {
    open();
    click("Create");
    await tick();
    expect($(".result").textContent).toMatch(/number/i);
    set("input[aria-label=Number]", "5");
    expect($(".result").textContent).toBe("");
  });

  it("deleting asks first and then calls the server", async () => {
    const remove = vi.spyOn(alertsApi, "remove").mockResolvedValue(saved());
    const { onDeleted } = open({ alert: saved() });
    click("Delete this alert");
    expect(remove).not.toHaveBeenCalled();
    click("Delete");
    await tick();
    expect(remove).toHaveBeenCalledWith("a1");
    expect(onDeleted).toHaveBeenCalled();
  });
});
