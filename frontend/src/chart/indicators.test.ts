import { describe, expect, it } from "vitest";
import type { IndicatorDef } from "../api";
import { paramProblem } from "../ui/indicatorDialogs";
import { indicatorLabel, inputKey, newEntry } from "./indicators";

// As GET /api/indicators describes the built-in EMA.
const EMA: IndicatorDef = {
  id: "ema",
  name: "EMA",
  title: "Moving Average Exponential",
  overlay: true,
  builtin: true,
  params: [
    { name: "length", type: "int", default: 9, label: "Length", group: "inputs", min: 1, max: 5000, step: 1 },
    { name: "source", type: "source", default: "close", label: "Source", group: "inputs", choices: ["open", "high", "low", "close", "hl2"] },
    { name: "color", type: "color", default: "#2962ff", label: "Colour", group: "style" },
    { name: "width", type: "width", default: 1, label: "Width", group: "style", min: 1, max: 4, step: 1 },
  ],
  outputs: [{ name: "ema", label: "EMA", color: "color", width: "width" }],
};

describe("indicators from their schema", () => {
  it("start with the defaults and show their inputs in the legend", () => {
    const entry = newEntry(EMA);
    expect(entry).toEqual({
      id: "ema",
      params: { length: 9, source: "close", color: "#2962ff", width: 1 },
      visible: true,
      price_labels: true,
      legend_values: true,
    });
    expect(indicatorLabel(EMA, entry.params)).toBe("EMA 9 close");
    expect(indicatorLabel(EMA, { ...entry.params, length: 21, source: "hl2", color: "#ff0000" })).toBe("EMA 21 hl2");
  });

  it("refetch values only when an input changes, not a colour or width", () => {
    const p = newEntry(EMA).params;
    expect(inputKey(EMA, { ...p, color: "#000000", width: 3 })).toBe(inputKey(EMA, p));
    expect(inputKey(EMA, { ...p, length: 10 })).not.toBe(inputKey(EMA, p));
  });

  it("check field values against the schema before they are applied", () => {
    const [length, source, color, width] = EMA.params;
    expect(paramProblem(length, 20)).toBeNull();
    expect(paramProblem(length, 0)).toBe("At least 1");
    expect(paramProblem(length, 2.5)).toBe("Enter a whole number");
    expect(paramProblem(length, NaN)).toBe("Enter a number");
    expect(paramProblem(length, 6000)).toBe("At most 5000");
    expect(paramProblem(source, "volume")).toBe("Pick a value");
    expect(paramProblem(color, "#12345g")).toBe("Pick a colour");
    expect(paramProblem(width, 4)).toBeNull();
  });
});
