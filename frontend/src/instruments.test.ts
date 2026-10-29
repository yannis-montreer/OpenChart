import { describe, expect, it } from "vitest";

import type { Instrument } from "./api";
import { chooseInstrument, removedInstruments, removedLine, sizeText, visibleInstruments } from "./instruments";

const row = (instrument: string, enabled: boolean): Instrument => ({
  instrument,
  source: "oanda",
  display_name: instrument,
  decimals: 5,
  pip_size: 0.0001,
  enabled,
  hours: null,
  closures: [],
});

const ALL = [row("XAU_USD", true), row("EUR_DKK", false), row("EUR_USD", true)];

describe("which instruments the chart offers", () => {
  it("leaves out the removed ones, keeping the order", () => {
    expect(visibleInstruments(ALL).map((i) => i.instrument)).toEqual(["XAU_USD", "EUR_USD"]);
  });

  it("lists the removed ones for Settings", () => {
    expect(removedInstruments(ALL).map((i) => i.instrument)).toEqual(["EUR_DKK"]);
  });

  it("opens the remembered instrument, but never a removed one", () => {
    expect(chooseInstrument(ALL, "EUR_USD")?.instrument).toBe("EUR_USD");
    expect(chooseInstrument(ALL, "EUR_DKK")?.instrument).toBe("XAU_USD");
    expect(chooseInstrument(ALL, "GONE")?.instrument).toBe("XAU_USD");
  });

  it("opens nothing when every instrument is removed", () => {
    expect(chooseInstrument([row("EUR_DKK", false)], "EUR_DKK")).toBeUndefined();
    expect(chooseInstrument([], undefined)).toBeUndefined();
  });
});

describe("the Removed section", () => {
  const stored = { bytes: 13_000_000, candles: 5, closures: 0, tick_files: 0, tick_bytes: 0, drawings: 1, alerts: 2, active_alerts: 2 };

  it("shows the stored size and the alerts that wait", () => {
    expect(removedLine({ ...row("EUR_DKK", false), stored })).toBe("EUR_DKK · about 12.4 MB stored · 2 active alerts paused until it is added back");
  });

  it("is only the name when nothing was measured", () => {
    expect(removedLine(row("EUR_DKK", false))).toBe("EUR_DKK");
  });

  it("writes sizes in units", () => {
    expect([0, 900, 2048, 5_000_000, 3 * 1024 ** 3].map(sizeText)).toEqual(["0 B", "900 B", "2.0 KB", "4.8 MB", "3.0 GB"]);
  });
});
