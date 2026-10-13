/**
 * Trading clock: seconds of market-open time (SPEC section 9, line geometry).
 *
 * It runs while the instrument's market is open and stops while it is closed
 * (weekend, daily break), on the trading hours' local clock. A drawn line's slope is
 * measured on it, so lines are straight on the chart across closures, and the alert
 * engine evaluates the same line. This is a copy of `openchart.market.sessions.
 * TradingClock`; `trading-time.fixture.json` (made by the Python tests) keeps the two
 * identical.
 */

import { tzOffset } from "../time";

export interface TradingHoursJson {
  tz: string;
  open: { day: string; time: string };
  close: { day: string; time: string };
  daily_breaks: { start: string; end: string }[];
}

export type Clock = (t: number) => number;

const DAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"];
const WEEK_MINUTES = 7 * 1440;
const WEEK_SECONDS = WEEK_MINUTES * 60;
const SUNDAY_EPOCH = 3 * 86400; // 1970-01-04 00:00, the first Sunday after the Unix epoch

const hhmm = (text: string) => {
  const [h, m] = text.split(":").map(Number);
  return h * 60 + m;
};
const weekMinute = (day: string, time: string) => ((DAYS.indexOf(day.toLowerCase()) + 1) % 7) * 1440 + hhmm(time);

function openAt(hours: TradingHoursJson, wm: number): boolean {
  const minuteOfDay = wm % 1440;
  const start = weekMinute(hours.open.day, hours.open.time);
  const end = weekMinute(hours.close.day, hours.close.time);
  const inWeek = start < end ? start <= wm && wm < end : !(end <= wm && wm < start);
  if (!inWeek) return false;
  for (const b of hours.daily_breaks ?? []) {
    const s = hhmm(b.start);
    const e = hhmm(b.end);
    if (s < e ? s <= minuteOfDay && minuteOfDay < e : !(e <= minuteOfDay && minuteOfDay < s)) return false;
  }
  return true;
}

/** The trading clock for these hours, or plain clock time if hours are unknown. */
export function tradingClock(hours: TradingHoursJson | null | undefined): Clock {
  if (!hours) return (t) => t;
  const open = new Uint8Array(WEEK_MINUTES);
  const prefix = new Float64Array(WEEK_MINUTES + 1);
  for (let m = 0; m < WEEK_MINUTES; m++) {
    open[m] = openAt(hours, m) ? 1 : 0;
    prefix[m + 1] = prefix[m] + open[m];
  }
  const weekOpenSeconds = prefix[WEEK_MINUTES] * 60;
  // The UTC offset only changes on whole half hours: cache it per half hour.
  const offsets = new Map<number, number>();
  const offset = (t: number) => {
    const key = Math.floor(t / 1800);
    let o = offsets.get(key);
    if (o === undefined) {
      o = tzOffset(t, hours.tz);
      if (offsets.size > 50_000) offsets.clear();
      offsets.set(key, o);
    }
    return o;
  };
  return (t: number) => {
    const local = t + offset(t);
    const shifted = local - SUNDAY_EPOCH;
    const week = Math.floor(shifted / WEEK_SECONDS);
    const rem = shifted - week * WEEK_SECONDS;
    const minute = Math.floor(rem / 60);
    const partial = open[minute] ? rem - minute * 60 : 0;
    return week * weekOpenSeconds + prefix[minute] * 60 + partial;
  };
}
