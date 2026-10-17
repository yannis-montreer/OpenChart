import { describe, expect, it } from "vitest";
import type { Drawing } from "../api";
import { bulkChange, canDeleteFromChart, canMove, cloneStyle, isHiddenDrawing, isLocked, patchBefore, rowTitle, visibleDrawings } from "./management";

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
