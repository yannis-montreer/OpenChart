// @vitest-environment happy-dom
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { ApiError, alertsApi, type Alert, type AlertEvent } from "../api";
import { AlertsPanel } from "./alertsPanel";

const base = {
  drawing_id: null, level: null, trigger_price: "mid", trigger_mode: "candle_close", timeframe: "1h", frequency: "once", cooldown_s: 0, expires_at: null,
  notify: ["app"], created_at: 1, updated_at: 1, last_fired_at: null, fire_count: 0,
};
function ind(id: string, over: Partial<Alert> = {}): Alert {
  return {
    ...base, id, instrument: "EUR_USD", type: "indicator", direction: "up", name: `alert ${id}`, status: "active", stop_reason: null,
    indicator: { uid: "u1", kind: "builtin", id: "rsi", params: {}, digest: "builtin", output: "rsi", condition: "cross_value", other_output: null, value: 30, name: "RSI(14)" },
    state: { last_bar: 1, armed: true, error: null, error_since: null, warnings: [], behind: false },
    ...over,
  } as Alert;
}
const line: Alert = { ...base, id: "L", instrument: "XAU_USD", type: "line_cross", direction: "cross", name: "gold line", status: "active", drawing_id: "d1" } as Alert;
const ev = (over: Partial<AlertEvent> = {}): AlertEvent => ({ id: 1, alert_id: "a", ts: 1700000000, price: 1.1, line_value: null, cross: "up", message: "m", late: null, detected_at: 1700000000, delivered: true, attempts: 1, error: null, ...over });

async function panel(alerts: Alert[], events: AlertEvent[] = []) {
  vi.spyOn(alertsApi, "list").mockResolvedValue(alerts);
  vi.spyOn(alertsApi, "events").mockResolvedValue(events);
  const p = new AlertsPanel();
  document.body.append(p.el);
  await p.load();
  return p;
}
const rows = () => [...document.querySelectorAll(".al-item")];
const tick = () => new Promise((r) => setTimeout(r, 0));

beforeEach(() => document.body.replaceChildren());
afterEach(() => vi.restoreAllMocks());

describe("indicator alerts in the panel, beside line alerts", () => {
  it("shows instrument, indicator, output, condition and state", async () => {
    await panel([line, ind("a")]);
    const text = rows().map((r) => r.textContent);
    expect(rows()).toHaveLength(2);
    const row = rows().find((r) => r.textContent?.includes("RSI(14)"))!;
    expect(row.textContent).toContain("EUR_USD");
    expect(row.textContent).toContain("RSI(14) rsi crossing up 30");
    expect(row.textContent).toContain("1h");
    expect(row.querySelector(".al-state")?.textContent).toBe("Armed");
    expect(text.find((t) => t?.includes("gold line"))).toContain("XAU_USD");
  });

  it("a stopped alert says why", async () => {
    await panel([ind("a", { status: "stopped", stop_reason: "the indicator was removed from the chart" })]);
    expect(rows()[0].querySelector(".al-state")?.textContent).toBe("Stopped");
    expect(rows()[0].querySelector(".al-reason")?.textContent).toBe("the indicator was removed from the chart");
    expect(rows()[0].classList.contains("status-stopped")).toBe(true);
  });

  it("an error says why and the alert is marked", async () => {
    await panel([ind("a", { state: { last_bar: 1, armed: true, error: "its output 'rsi' is gone from the file", error_since: 1, warnings: [], behind: false } })]);
    expect(rows()[0].querySelector(".al-state")?.textContent).toBe("Error");
    expect(rows()[0].querySelector(".al-reason")?.textContent).toBe("its output 'rsi' is gone from the file");
    expect(rows()[0].classList.contains("status-error")).toBe(true);
  });

  it("shows warnings and a behind note on the alert", async () => {
    await panel([ind("a", { state: { last_bar: 1, armed: true, error: null, error_since: null, warnings: ["lookahead"], behind: true } })]);
    expect(rows()[0].querySelectorAll(".al-warn")).toHaveLength(1);
    expect(rows()[0].querySelector(".al-warn")?.textContent).toBe("lookahead");
    expect(rows()[0].querySelector(".al-warn")?.getAttribute("data-tip")).toMatch(/later bars/i);
    expect(rows()[0].querySelector(".al-behind")?.textContent).toMatch(/behind/i);
  });

  it("stops and restarts an alert with the right status", async () => {
    const update = vi.spyOn(alertsApi, "update");
    const p = await panel([ind("a")]);
    update.mockResolvedValueOnce(ind("a", { status: "stopped", stop_reason: "stopped by you" }));
    (rows()[0].querySelector("[aria-label='Stop this alert']") as HTMLElement).click();
    await tick();
    expect(update).toHaveBeenLastCalledWith("a", { status: "stopped" });
    expect(rows()[0].querySelector(".al-reason")?.textContent).toBe("stopped by you");
    update.mockResolvedValueOnce(ind("a"));
    (rows()[0].querySelector("[aria-label='Restart this alert']") as HTMLElement).click();
    await tick();
    expect(update).toHaveBeenLastCalledWith("a", { status: "active" });
    expect(rows()[0].querySelector(".al-state")?.textContent).toBe("Armed");
    expect(p).toBeTruthy();
  });

  it("when the server refuses a restart, says why in words (not a browser alert box)", async () => {
    const stopped = ind("a", { status: "stopped", stop_reason: "the indicator was removed from the chart" });
    vi.spyOn(alertsApi, "update").mockRejectedValue(new ApiError(400, "no indicator 'u1' on the EUR_USD chart"));
    const box = vi.fn();
    (window as unknown as { alert: () => void }).alert = box;
    await panel([stopped]);
    (rows()[0].querySelector("[aria-label='Restart this alert']") as HTMLElement).click();
    await tick();
    expect(box).not.toHaveBeenCalled();
    expect(document.querySelector(".toast")?.textContent).toContain("no indicator 'u1' on the EUR_USD chart");
    expect(rows()[0].querySelector(".al-state")?.textContent).toBe("Stopped");
  });

  it("edit asks the app to open the right dialog; delete asks first", async () => {
    const remove = vi.spyOn(alertsApi, "remove").mockResolvedValue(ind("a"));
    const p = await panel([ind("a")]);
    const onEdit = vi.fn();
    p.onEdit = onEdit;
    (rows()[0].querySelector("[aria-label=Edit]") as HTMLElement).click();
    expect(onEdit).toHaveBeenCalledWith(expect.objectContaining({ id: "a" }));
    (rows()[0].querySelector("[aria-label=Delete]") as HTMLElement).click();
    expect(remove).not.toHaveBeenCalled();
    [...document.querySelectorAll(".confirm-dialog button")].find((b) => b.textContent === "Delete")!.dispatchEvent(new MouseEvent("click", { bubbles: true }));
    await tick();
    expect(remove).toHaveBeenCalledWith("a");
    expect(rows()).toHaveLength(0);
  });

  it("lists the alerts of one instance for the legend's bell", async () => {
    const p = await panel([ind("a"), ind("b", { indicator: { ...ind("a").indicator!, uid: "u2" } }), line]);
    expect(p.forInstance("EUR_USD", "u1").map((a) => a.id)).toEqual(["a"]);
  });
});

describe("the log shows what the server sent", () => {
  it("an indicator fire: the server's message text, and the late label", async () => {
    await panel(
      [ind("a")],
      [ev({ alert_id: "a", message: "EUR_USD 1h RSI(14) crossed up 30.00 at bar close 14:00", bar: 1000, value: 31, other_value: null, late: "worker" })],
    );
    (document.querySelectorAll(".al-tabs button")[1] as HTMLElement).click();
    const row = document.querySelector(".al-event")!;
    expect(row.querySelector(".al-message")?.textContent).toBe("EUR_USD 1h RSI(14) crossed up 30.00 at bar close 14:00");
    expect(row.querySelector(".al-late")?.textContent).toContain("after the indicator could not be computed");
  });

  it("a line fire keeps its price and line value", async () => {
    await panel([line], [ev({ alert_id: "L", price: 2000, line_value: 1999.5 })]);
    (document.querySelectorAll(".al-tabs button")[1] as HTMLElement).click();
    expect(document.querySelector(".al-event")?.textContent).toContain("line");
    expect(document.querySelector(".al-event .al-message")).toBeNull();
  });
});
