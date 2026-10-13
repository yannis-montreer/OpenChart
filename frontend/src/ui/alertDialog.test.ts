import { describe, expect, it } from "vitest";
import { triggerFields, triggerOf } from "./alertDialog";

describe("alert dialog triggers (SPEC section 9 trigger options)", () => {
  it("maps each choice to the spec's frequency and mode, and back", () => {
    expect(triggerFields("once")).toEqual({ frequency: "once", trigger_mode: "touch", cooldown_s: 0 });
    expect(triggerFields("per_bar")).toEqual({ frequency: "once_per_bar", trigger_mode: "touch", cooldown_s: 0 });
    expect(triggerFields("bar_close")).toEqual({ frequency: "repeating", trigger_mode: "candle_close", cooldown_s: 0 });
    expect(triggerFields("per_minute")).toEqual({ frequency: "repeating", trigger_mode: "touch", cooldown_s: 60 });
    for (const t of ["once", "per_bar", "bar_close", "per_minute"] as const) expect(triggerOf(triggerFields(t))).toBe(t);
  });
});
