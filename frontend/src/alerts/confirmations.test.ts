import { describe, expect, it } from "vitest";

import { ApiError, apiErrorFrom, type Alert, type DefinitionUsage } from "../api";
import { definitionAlertIds, definitionAlertLines, layoutApplyPrompt, previewOf, removeInstancePrompt } from "./confirmations";

const preview = (stopped: Array<{ id: string; name: string; uid: string; reason: string }> = [], rearmed: Array<{ id: string; name: string; uid: string; reason: string }> = []) => ({
  instrument: "EUR_USD",
  alerts_stopped: stopped,
  alerts_rearmed: rearmed,
});
const S = (id: string, name = `alert ${id}`) => ({ id, name, uid: "u1", reason: `the layout "Gold" applied to the chart does not have its indicator` });

describe("applying a layout that would stop alerts", () => {
  it("needs no question when nothing stops, and sends no ids", () => {
    const p = layoutApplyPrompt("Gold H1", preview());
    expect(p).toMatchObject({ needed: false, ids: [] });
  });

  it("lists each alert, says they will stop, and sends exactly their ids", () => {
    const p = layoutApplyPrompt("Gold H1", preview([S("a"), S("b", "RSI under 30")]));
    expect(p.needed).toBe(true);
    expect(p.ids).toEqual(["a", "b"]);
    expect(p.title).toBe("Apply “Gold H1”?");
    expect(p.text).toBe("Applying this layout to EUR_USD stops 2 indicator alerts. They stay in the Alerts panel, stopped, with their settings.");
    expect(p.lines).toEqual(["alert a", "RSI under 30"]);
    expect(p.confirm).toBe("Apply and stop 2 alerts");
  });

  it("says one alert in the singular", () => {
    const p = layoutApplyPrompt("Gold H1", preview([S("a")]));
    expect(p.text).toMatch(/stops 1 indicator alert\. They/);
    expect(p.text).not.toMatch(/alerts\./);
    expect(p.confirm).toBe("Apply and stop 1 alert");
  });

  it("mentions the alerts that restart armed", () => {
    const p = layoutApplyPrompt("Gold H1", preview([S("a")], [S("r", "EMA cross")]));
    expect(p.rearmed).toEqual(["EMA cross"]);
    expect(p.text).toMatch(/1 other alert starts over/i);
    expect(layoutApplyPrompt("Gold H1", preview([], [S("r")])).needed).toBe(false);
  });

  it("reads the preview from the server's 409, and from nothing else", () => {
    const body = { detail: { message: "Applying it stops 1 indicator alert", preview: preview([S("a")]) } };
    expect(previewOf(apiErrorFrom(409, "Conflict", body))?.alerts_stopped[0].id).toBe("a");
    expect(previewOf(apiErrorFrom(409, "Conflict", { detail: "no" }))).toBeNull();
    expect(previewOf(apiErrorFrom(404, "x", body))).toBeNull();
    expect(previewOf(new Error("x"))).toBeNull();
    expect(previewOf(new ApiError(409, "x"))).toBeNull();
  });
});

describe("removing an indicator from a chart", () => {
  const A = (id: string, over: Partial<Alert> = {}) => ({ id, name: `n${id}`, status: "active", state: { error: null }, ...over }) as unknown as Alert;

  it("needs no question when no alert runs on it", () => {
    expect(removeInstancePrompt("RSI(14)", [])).toMatchObject({ needed: false });
    expect(removeInstancePrompt("RSI(14)", [A("a", { status: "stopped" }), A("b", { status: "triggered" }), A("c", { status: "expired" })]).needed).toBe(false);
  });

  it("names the alerts that will stop: active ones, including those in error", () => {
    const p = removeInstancePrompt("RSI(14)", [A("a"), A("b", { state: { error: "x" } } as never), A("c", { status: "stopped" })]);
    expect(p.needed).toBe(true);
    expect(p.lines).toEqual(["na", "nb"]);
    expect(p.text).toBe("Removing RSI(14) stops 2 alerts. They stay in the Alerts panel, stopped, with their settings.");
    expect(p.confirm).toBe("Remove and stop 2 alerts");
  });

  it("singular", () => {
    expect(removeInstancePrompt("RSI(14)", [A("a")]).text).toBe("Removing RSI(14) stops 1 alert. It stays in the Alerts panel, stopped, with its settings.");
  });
});

describe("deleting a definition that has alerts", () => {
  const usage = (alerts: DefinitionUsage["alerts"]): DefinitionUsage => ({ id: "mine", in_use: true, charts: [], layouts: [], alerts });
  const al = (id: string, status = "active") => ({ id, name: `alert ${id}`, instrument: "EUR_USD", uid: "u1", status });

  it("lists them with instrument and status", () => {
    expect(definitionAlertLines(usage([al("a"), al("b", "stopped")]))).toEqual(["alert a (EUR_USD)", "alert b (EUR_USD, stopped)"]);
  });

  it("names every one, in the order the server gave", () => {
    expect(definitionAlertIds(usage([al("a"), al("b")]))).toEqual(["a", "b"]);
    expect(definitionAlertIds({ id: "x", in_use: false, charts: [], layouts: [] })).toEqual([]);
  });
});
