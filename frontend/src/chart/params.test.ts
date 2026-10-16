import { describe, expect, it } from "vitest";

import type { IndicatorDef, IndicatorParam } from "../api";
import { paramProblem, reconcileParams } from "./params";

const P = (p: Partial<IndicatorParam> & Pick<IndicatorParam, "name" | "type" | "default">): IndicatorParam => ({ label: p.name, group: "inputs", ...p });

const def = (params: IndicatorParam[]): IndicatorDef => ({ id: "x", name: "X", title: "X", overlay: true, builtin: false, params, outputs: [] });

const DEF = def([
  P({ name: "length", type: "int", default: 9, min: 1, max: 100 }),
  P({ name: "mult", type: "float", default: 2, min: 0.1 }),
  P({ name: "on", type: "bool", default: true }),
  P({ name: "mode", type: "choice", default: "a", choices: ["a", "b"] }),
  P({ name: "source", type: "source", default: "close", choices: ["open", "close"] }),
  P({ name: "tag", type: "string", default: "hi", max_length: 4 }),
  P({ name: "color", type: "color", default: "#2962ff", group: "style" }),
  P({ name: "width", type: "width", default: 2, min: 1, max: 4, group: "style" }),
]);

const GOOD = { length: 20, mult: 1.5, on: false, mode: "b", source: "open", tag: "abcd", color: "#112233", width: 3 };

describe("what a layout stored, against what the file declares now", () => {
  it("keeps every value that is still valid", () => {
    expect(reconcileParams(DEF, GOOD)).toEqual(GOOD);
  });

  it("drops a parameter the file no longer has (an input that was renamed or removed)", () => {
    expect(reconcileParams(DEF, { ...GOOD, old_input: 3 })).toEqual(GOOD);
  });

  it("fills a parameter the layout does not have with its default (an input that was added)", () => {
    expect(reconcileParams(DEF, { length: 20 })).toEqual({ length: 20, mult: 2, on: true, mode: "a", source: "close", tag: "hi", color: "#2962ff", width: 2 });
    expect(reconcileParams(DEF, {})).toEqual(reconcileParams(DEF, undefined as never));
  });

  it("replaces a value that is no longer acceptable by the default (an input that changed type or range)", () => {
    const stored = { length: "nine", mult: 0, on: "yes", mode: "c", source: "volume", tag: "too long", color: "red", width: 9 };
    expect(reconcileParams(DEF, stored)).toEqual({ length: 9, mult: 2, on: true, mode: "a", source: "close", tag: "hi", color: "#2962ff", width: 2 });
  });

  it("replaces each bad value on its own and keeps the good ones next to it", () => {
    expect(reconcileParams(DEF, { ...GOOD, length: 1000, mode: "z" })).toEqual({ ...GOOD, length: 9, mode: "a" });
  });

  it("does not accept a whole number written as text, a NaN or an infinity", () => {
    expect(reconcileParams(DEF, { ...GOOD, length: "20" }).length).toBe(9);
    expect(reconcileParams(DEF, { ...GOOD, mult: Number.NaN }).mult).toBe(2);
    expect(reconcileParams(DEF, { ...GOOD, mult: Number.POSITIVE_INFINITY }).mult).toBe(2);
  });

  it("changes nothing for an indicator with no parameters", () => {
    expect(reconcileParams(def([]), { stale: 1 })).toEqual({});
  });
});

describe("paramProblem moved here with its tests unchanged", () => {
  it("still says why", () => {
    expect(paramProblem(DEF.params[0], 0)).toBe("At least 1");
  });
});
