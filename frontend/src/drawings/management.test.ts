import { describe, expect, it } from "vitest";
import type { Alert, Drawing } from "../api";
import { alertBody, bulkChange, restorableAlerts, restoreSummary, canDeleteFromChart, canMove, cloneStyle, isHiddenDrawing, isLocked, patchBefore, rowTitle, visibleDrawings } from "./management";

const d = (id: string, style: Partial<Drawing["style"]> = {}, over: Partial<Drawing> = {}): Drawing => ({
  id,
  instrument: "XAU_USD",
  type: "rect",
  points: [
    { t: 1, p: 1 },
    { t: 2, p: 2 },
  ],
  style: { color: "#4d8dff", width: 2, dash: "solid", ...style },
  label: null,
  created_at: 0,
  updated_at: 0,
  ...over,
});

describe("lock rules", () => {
  it("treats a drawing without the flag as unlocked and visible", () => {
    expect(isLocked(d("a"))).toBe(false);
    expect(isHiddenDrawing(d("a"))).toBe(false);
    expect(isHiddenDrawing(d("a", { hidden: false }))).toBe(false);
  });

  it("does not let a locked drawing be moved or deleted from the chart", () => {
    const locked = d("a", { locked: true });
    expect(canMove(locked)).toBe(false);
    expect(canDeleteFromChart(locked)).toBe(false);
    expect(canMove(d("b", { locked: false }))).toBe(true);
    expect(canDeleteFromChart(d("b"))).toBe(true);
  });

  it("is independent of hiding", () => {
    const x = d("a", { hidden: true });
    expect(isHiddenDrawing(x)).toBe(true);
    expect(canMove(x)).toBe(true);
    expect(isLocked(x)).toBe(false);
  });
});

describe("visibleDrawings", () => {
  const list = [d("a"), d("b", { hidden: true }), d("c", { locked: true })];

  it("leaves out the hidden ones", () => {
    expect(visibleDrawings(list, false).map((x) => x.id)).toEqual(["a", "c"]);
  });

  it("shows none while all drawings are hidden", () => {
    expect(visibleDrawings(list, true)).toEqual([]);
  });
});

describe("bulkChange", () => {
  const list = [d("a"), d("b", { locked: true }), d("c", { locked: false })];

  it("names only the drawings whose flag would change", () => {
    expect(bulkChange(list, "locked", true)).toEqual(["a", "c"]);
    expect(bulkChange(list, "locked", false)).toEqual(["b"]);
    expect(bulkChange(list, "hidden", false)).toEqual([]);
    expect(bulkChange(list, "hidden", true)).toEqual(["a", "b", "c"]);
  });
});

describe("patchBefore", () => {
  it("restores the points", () => {
    const x = d("a");
    expect(patchBefore(x, { points: [{ t: 5, p: 5 }, { t: 6, p: 6 }] })).toEqual({ points: x.points });
  });

  it("restores only the style keys the patch touches", () => {
    expect(patchBefore(d("a"), { style: { color: "#ff0000" } })).toEqual({ style: { color: "#4d8dff" } });
    expect(patchBefore(d("a", { width: 3 }), { style: { width: 1, dash: "dotted" } })).toEqual({ style: { width: 3, dash: "solid" } });
  });

  it("restores a key that was never stored as its default", () => {
    expect(patchBefore(d("a"), { style: { locked: true, hidden: true, extend: true } })).toEqual({
      style: { locked: false, hidden: false, extend: false },
    });
    expect(patchBefore(d("a"), { style: { fill: 40 } })).toEqual({ style: { fill: 15 } }); // a rectangle's default
    expect(patchBefore(d("a", {}, { type: "channel" }), { style: { fill: 40 } })).toEqual({ style: { fill: 10 } });
    expect(patchBefore(d("a", { fill: 30 }), { style: { fill: 40 } })).toEqual({ style: { fill: 30 } });
  });

  it("restores the label, or clears it when there was none", () => {
    expect(patchBefore(d("a"), { label: "new" })).toEqual({ clear_label: true });
    expect(patchBefore(d("a", {}, { label: "old" }), { label: "new" })).toEqual({ label: "old" });
    expect(patchBefore(d("a", {}, { label: "old" }), { clear_label: true })).toEqual({ label: "old" });
  });

  it("combines what a patch changes", () => {
    const x = d("a", {}, { label: "old" });
    expect(patchBefore(x, { points: [], style: { dash: "dashed" }, label: "n" })).toEqual({
      points: x.points,
      style: { dash: "solid" },
      label: "old",
    });
  });
});

describe("cloneStyle", () => {
  it("does not copy the lock or the hiding", () => {
    expect(cloneStyle({ color: "#fff", width: 2, dash: "solid", fill: 20, locked: true, hidden: true })).toEqual({
      color: "#fff",
      width: 2,
      dash: "solid",
      fill: 20,
    });
  });
});

describe("rowTitle", () => {
  it("names the tool and its label", () => {
    expect(rowTitle(d("a"))).toBe("Rectangle");
    expect(rowTitle(d("a", {}, { label: "Range" }))).toBe("Rectangle: Range");
    expect(rowTitle(d("a", {}, { type: "text", label: "Support zone" }))).toBe("Text: Support zone");
    expect(rowTitle(d("a", {}, { type: "hline", points: [{ t: 1, p: 2650.5 }] }))).toBe("Horizontal line 2650.5");
  });
});

const al = (status: Alert["status"], over: Partial<Alert> = {}): Alert => ({
  id: "x",
  instrument: "XAU_USD",
  type: "line_cross",
  drawing_id: "old",
  level: null,
  direction: "down",
  trigger_price: "bid",
  trigger_mode: "candle_close",
  timeframe: "15m",
  frequency: "repeating",
  cooldown_s: 60,
  name: "My alert",
  expires_at: 4102444800,
  notify: ["telegram"],
  status,
  created_at: 5,
  updated_at: 6,
  last_fired_at: 7,
  fire_count: 3,
  ...over,
});

describe("restoring a line's alerts after an undo", () => {
  it("restores the active and the stopped ones, not the finished ones", () => {
    const list = [al("active", { id: "a" }), al("stopped", { id: "b" }), al("triggered", { id: "c" }), al("expired", { id: "d" })];
    const { restore, skipped } = restorableAlerts(list);
    expect(restore.map((a) => a.id)).toEqual(["a", "b"]);
    expect(skipped.map((a) => a.id)).toEqual(["c", "d"]);
  });

  it("re-creates an alert with its settings on the new drawing, and nothing of its history", () => {
    expect(alertBody(al("active"), "new")).toEqual({
      instrument: "XAU_USD",
      type: "line_cross",
      drawing_id: "new",
      direction: "down",
      trigger_price: "bid",
      trigger_mode: "candle_close",
      timeframe: "15m",
      frequency: "repeating",
      cooldown_s: 60,
      name: "My alert",
      expires_at: 4102444800,
      notify: ["telegram"],
    });
  });

  it("leaves out an expiry that was never set", () => {
    expect("expires_at" in alertBody(al("active", { expires_at: null }), "new")).toBe(false);
  });

  it("says what happened to the alerts", () => {
    expect(restoreSummary(0, 0, 0)).toBeNull();
    expect(restoreSummary(1, 0, 0)).toBe("Drawing restored with its alert, armed again. The history of its fires is not restored.");
    expect(restoreSummary(2, 0, 0)).toBe("Drawing restored with its 2 alerts, armed again. The history of their fires is not restored.");
    expect(restoreSummary(1, 1, 0)).toBe("Drawing restored. 1 of 2 alerts could not be restored.");
    expect(restoreSummary(0, 2, 0)).toBe("Drawing restored. 2 of 2 alerts could not be restored.");
    expect(restoreSummary(1, 0, 2)).toBe("Drawing restored with its alert, armed again. The history of its fires is not restored. 2 finished alerts (triggered or expired) were not restored.");
    expect(restoreSummary(0, 0, 1)).toBe("Drawing restored. 1 finished alert (triggered or expired) was not restored.");
  });
});

describe("rowTitle with alerts", () => {
  it("is unchanged by them (the bell is a marker beside the row)", () => {
    expect(rowTitle(d("a"))).toBe("Rectangle");
  });
});
