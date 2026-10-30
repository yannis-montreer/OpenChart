import { describe, expect, it } from "vitest";

import type { DeletePreview, Instrument } from "./api";
import {
  canDelete,
  chooseInstrument,
  deletedText,
  deleteLines,
  deleteSize,
  drawingsAndAlerts,
  removedInstruments,
  removedLine,
  sizeText,
  visibleInstruments,
} from "./instruments";

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

describe("Delete data… on a removed instrument", () => {
  const stored = {
    bytes: 13_000_000,
    candles: 1_201_000,
    m1: 1_000_000,
    h1: 200_000,
    cached: 1_000,
    closures: 12,
    tick_files: 3,
    tick_bytes: 2048,
    drawings: 1,
    alerts: 2,
    active_alerts: 1,
  };
  const preview = (over: Partial<typeof stored> = {}): DeletePreview => ({
    instrument: "EUR_DKK",
    stored: { ...stored, ...over },
    needs_drawings_and_alerts: (over.drawings ?? stored.drawings) + (over.alerts ?? stored.alerts) > 0,
    token: "t",
  });

  it("lists what goes, with the size, and what stays unless ticked", () => {
    expect(deleteLines(preview(), false)).toEqual([
      "1,000,000 minute and 200,000 hourly candles, 1,000 cached bars",
      "12 market closures",
      "3 tick files (2.0 KB)",
      "Kept unless the box below is ticked: 1 drawing and 2 alerts",
    ]);
    expect(deleteLines(preview(), true).slice(-1)).toEqual(["1 drawing and 2 alerts, and their alert log"]);
    expect(deleteSize(preview())).toBe("about 12.4 MB");
    expect([drawingsAndAlerts(1, 0), drawingsAndAlerts(0, 3), drawingsAndAlerts(2, 1)]).toEqual(["1 drawing", "3 alerts", "2 drawings and 1 alert"]);
  });

  it("leaves out what there is none of", () => {
    expect(deleteLines(preview({ closures: 0, tick_files: 0, tick_bytes: 0, drawings: 0, alerts: 0 }), false)).toEqual([
      "1,000,000 minute and 200,000 hourly candles, 1,000 cached bars",
    ]);
  });

  it("needs the exact name typed, and the box when there are drawings or alerts", () => {
    expect(canDelete(preview(), "EUR_DKK", true)).toBe(true);
    expect(canDelete(preview(), "EUR_DKK", false)).toBe(false);
    expect(canDelete(preview({ drawings: 0, alerts: 0 }), "EUR_DKK", false)).toBe(true);
    for (const typed of ["", "eur_dkk", "EUR_DKK ", "EUR_DK", " EUR_DKK"]) expect(canDelete(preview(), typed, true)).toBe(false);
  });

  it("says what was deleted and how much the database shrank", () => {
    const done = { ok: true as const, deleted: {}, drawings: 1, alerts: 2, tick_error: null, vacuum: { before: 50e6, after: 30e6, seconds: 1.2 }, vacuum_error: null };
    expect(deletedText("EUR_DKK", done)).toBe("EUR_DKK deleted, with 1 drawing and 2 alerts. The database went from 47.7 MB to 28.6 MB.");
    expect(deletedText("EUR_DKK", { ...done, drawings: 0, alerts: 0, vacuum: null, vacuum_error: "busy" })).toBe(
      "EUR_DKK deleted. The database was not compacted (busy); SQLite reuses the space.",
    );
    expect(deletedText("EUR_DKK", { ...done, drawings: 0, alerts: 0, tick_error: "PermissionError" })).toContain("Some tick files could not be deleted");
  });
});
