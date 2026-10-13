import { describe, expect, it } from "vitest";
import {
  chartTime,
  formatChartTime,
  formatCountdown,
  offsetLabel,
  toChartTimes,
  tradingDate,
  tzOffset,
} from "./time";

const SESSION = { timezone: "America/New_York", day_anchor_hour: 17, week_anchor_weekday: 4 };
const utc = (...a: [number, number, number, number?, number?]) =>
  Date.UTC(a[0], a[1] - 1, a[2], a[3] ?? 0, a[4] ?? 0) / 1000;

describe("tzOffset", () => {
  it("follows DST", () => {
    expect(tzOffset(utc(2024, 1, 10, 12), "America/New_York")).toBe(-5 * 3600);
    expect(tzOffset(utc(2024, 7, 10, 12), "America/New_York")).toBe(-4 * 3600);
    expect(tzOffset(utc(2024, 7, 10, 12), "Europe/Oslo")).toBe(2 * 3600);
    expect(tzOffset(utc(2024, 7, 10, 12), "UTC")).toBe(0);
    expect(tzOffset(utc(2024, 7, 10, 12), "Asia/Kolkata")).toBe(5.5 * 3600);
  });

  it("labels offsets", () => {
    expect(offsetLabel(utc(2024, 7, 10), "Europe/Oslo")).toBe("UTC+2");
    expect(offsetLabel(utc(2024, 7, 10), "Asia/Kolkata")).toBe("UTC+5:30");
    expect(offsetLabel(utc(2024, 1, 10), "America/New_York")).toBe("UTC-5");
    expect(offsetLabel(utc(2024, 1, 10), "UTC")).toBe("UTC");
  });
});

describe("chart times", () => {
  it("daily bars show their trading date", () => {
    // Monday's bar opens Sunday 17:00 New York (21:00 UTC in summer).
    const sundayOpen = utc(2024, 7, 14, 21);
    expect(tradingDate(sundayOpen, SESSION)).toBe(utc(2024, 7, 15));
    expect(chartTime(sundayOpen, "1D", "Europe/Oslo", SESSION)).toBe(utc(2024, 7, 15));
    // Winter: 17:00 New York is 22:00 UTC.
    expect(chartTime(utc(2024, 1, 9, 22), "1D", "UTC", SESSION)).toBe(utc(2024, 1, 10));
  });

  it("weekly bars show the Monday of their week", () => {
    const fridayClose = utc(2024, 7, 5, 21); // Friday 17:00 New York
    expect(chartTime(fridayClose, "1W", "UTC", SESSION)).toBe(utc(2024, 7, 8));
  });

  it("intraday bars are shifted to the display timezone", () => {
    const t = utc(2024, 7, 10, 12);
    expect(chartTime(t, "15m", "Europe/Oslo", SESSION)).toBe(t + 7200);
    expect(chartTime(t, "15m", "UTC", SESSION)).toBe(t);
  });

  it("drops bars that would repeat when the display zone leaves DST", () => {
    // Europe/Oslo falls back on 2024-10-27 at 01:00 UTC: 02:00-03:00 local happens twice.
    const start = utc(2024, 10, 27, 0, 0);
    const bars = Array.from({ length: 12 }, (_, i) => ({ t: start + i * 900 }));
    const { bars: kept, times } = toChartTimes(bars, "15m", "Europe/Oslo", SESSION);
    expect(times.every((t, i) => i === 0 || t > times[i - 1])).toBe(true);
    expect(kept.length).toBe(8); // 4 bars in the repeated hour are dropped
  });
});

describe("formatting", () => {
  it("formats crosshair labels", () => {
    expect(formatChartTime(utc(2026, 10, 13, 2, 15), "15m")).toBe("Tue 13 Oct '26  02:15");
    expect(formatChartTime(utc(2026, 10, 13), "1D")).toBe("Tue 13 Oct '26");
  });

  it("formats countdowns", () => {
    expect(formatCountdown(299.7)).toBe("04:59");
    expect(formatCountdown(3723)).toBe("1:02:03");
    expect(formatCountdown(-5)).toBe("00:00");
  });
});
