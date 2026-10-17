import { describe, expect, it } from "vitest";

import type { SeriesPlan } from "./indicatorOutputs";
import { dashPlans, previewLabel } from "./previewPlan";

const plans: SeriesPlan[] = [
  { name: "a", kind: "line", create: { priceLineVisible: false }, options: { color: "#f00", lineWidth: 2, lineStyle: 0, lastValueVisible: true, visible: true } },
  { name: "b", kind: "area", create: {}, options: { lineColor: "#0f0", lastValueVisible: true } },
  { name: "c", kind: "histogram", create: {}, options: { color: "#00f", lastValueVisible: true } },
];

describe("dashPlans", () => {
  it("draws lines and areas dashed, whatever style the file asked for", () => {
    const [a, b] = dashPlans(plans);
    expect(a.options.lineStyle).toBe(2);
    expect(b.options.lineStyle).toBe(2);
  });

  it("keeps a preview off the price scale's labels", () => {
    for (const p of dashPlans(plans)) expect(p.options.lastValueVisible).toBe(false);
  });

  it("keeps colours, widths and visibility, and does not change its input", () => {
    const copy = structuredClone(plans);
    const out = dashPlans(plans);
    expect(plans).toEqual(copy);
    expect(out[0].options).toMatchObject({ color: "#f00", lineWidth: 2, visible: true });
    expect(out[2].options.color).toBe("#00f");
    expect(out.map((p) => p.name)).toEqual(["a", "b", "c"]);
  });

  it("leaves a histogram solid (it has no line to dash)", () => {
    expect(dashPlans(plans)[2].options.lineStyle).toBeUndefined();
  });

  it("is empty for no plans", () => {
    expect(dashPlans([])).toEqual([]);
  });
});

describe("previewLabel", () => {
  it("marks the legend row as a preview", () => {
    expect(previewLabel("SMA cross 20 close")).toBe("Preview: SMA cross 20 close");
  });
});
