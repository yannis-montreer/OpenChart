import { describe, expect, it } from "vitest";

import {
  DEFAULT_M1_DEPTH,
  DEFAULT_TICK_RETENTION,
  M1_DEPTHS,
  TICK_RETENTION,
  depthLabel,
  depthLine,
  depthSaved,
  tickLabel,
  utcDate,
} from "./retention";

describe("retention choices", () => {
  it("match the server's (retention.TICK_RETENTION, retention.M1_DEPTHS)", () => {
    expect([...TICK_RETENTION]).toEqual(["7d", "30d", "90d", "1y", "forever", "off"]);
    expect([...M1_DEPTHS]).toEqual(["3m", "1y", "2y"]);
    expect(DEFAULT_TICK_RETENTION).toBe("30d");
    expect(DEFAULT_M1_DEPTH).toBe("2y");
  });

  it("labels the defaults", () => {
    expect(tickLabel("30d")).toBe("30 days (default)");
    expect(tickLabel("off")).toBe("Don't store ticks");
    expect(tickLabel("forever")).toBe("Forever");
    expect(depthLabel("2y")).toBe("2 years (default)");
    expect(depthLabel("3m")).toBe("3 months");
    expect(tickLabel("weird")).toBe("weird");
  });

  it("says how far back, in UTC", () => {
    const ts = Date.UTC(2025, 9, 14, 23, 59) / 1000;
    expect(utcDate(ts)).toBe("14 Oct 2025");
    expect(depthLine("1y", ts)).toBe("Minute history: 1 year (back to 14 Oct 2025)");
    expect(utcDate(Date.UTC(2025, 9, 15, 0, 0) / 1000)).toBe("15 Oct 2025");
  });

  it("says what saving does", () => {
    expect(depthSaved("XAU_USD", "2y", true)).toContain("download");
    expect(depthSaved("XAU_USD", "3m", false)).toContain("deleted just after midnight UTC");
  });
});
