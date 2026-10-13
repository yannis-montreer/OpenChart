/**
 * Time handling for the chart.
 *
 * The server sends UTC epoch seconds, and a bar's `t` is its start. Lightweight Charts
 * draws times as UTC, so to show a display timezone we shift each timestamp by that
 * zone's UTC offset at that instant ("wall-clock seconds"). Daily and weekly bars are
 * shown on their trading date instead: a daily bar that opens Sunday 17:00 New York is
 * Monday's bar.
 */

export interface SessionMeta {
  timezone: string;
  day_anchor_hour: number;
  week_anchor_weekday: number;
}

const DAY = 86400;
const formatters = new Map<string, Intl.DateTimeFormat>();

function formatter(tz: string): Intl.DateTimeFormat {
  let f = formatters.get(tz);
  if (!f) {
    f = new Intl.DateTimeFormat("en-US", {
      timeZone: tz,
      hourCycle: "h23",
      year: "numeric",
      month: "numeric",
      day: "numeric",
      hour: "numeric",
      minute: "numeric",
      second: "numeric",
    });
    formatters.set(tz, f);
  }
  return f;
}

/** UTC offset of `tz` at the instant `utc` (epoch seconds), in seconds. */
export function tzOffset(utc: number, tz: string): number {
  if (tz === "UTC") return 0;
  const parts: Record<string, number> = {};
  for (const p of formatter(tz).formatToParts(new Date(utc * 1000))) {
    if (p.type !== "literal") parts[p.type] = Number(p.value);
  }
  const wall = Date.UTC(parts.year, parts.month - 1, parts.day, parts.hour, parts.minute, parts.second);
  return Math.round(wall / 1000 - Math.floor(utc));
}

/** Short label such as "UTC+2" or "UTC-4:30" for `tz` at `utc`. */
export function offsetLabel(utc: number, tz: string): string {
  const off = tzOffset(utc, tz);
  if (off === 0) return "UTC";
  const sign = off > 0 ? "+" : "-";
  const h = Math.floor(Math.abs(off) / 3600);
  const m = Math.round((Math.abs(off) % 3600) / 60);
  return `UTC${sign}${h}${m ? ":" + String(m).padStart(2, "0") : ""}`;
}

/** Trading date (as UTC midnight seconds) of the bar starting at `t`. */
export function tradingDate(t: number, session: SessionMeta): number {
  const shifted = t + tzOffset(t, session.timezone) + (24 - session.day_anchor_hour) * 3600;
  return Math.floor(shifted / DAY) * DAY;
}

/**
 * Chart x-coordinate for a bar of timeframe `tf` starting at `t`.
 * Daily bars: their trading date. Weekly bars: the Monday of their week.
 * Everything else: wall-clock seconds in the display timezone `tz`.
 */
export function chartTime(t: number, tf: string, tz: string, session: SessionMeta): number {
  if (tf === "1D") return tradingDate(t, session);
  if (tf === "1W") {
    // The week opens on the anchor weekday's close (Friday 17:00): +3 days is Monday.
    const opensOn = tradingDate(t, session);
    const weekday = (new Date(opensOn * 1000).getUTCDay() + 6) % 7; // Monday = 0
    const toMonday = (7 - weekday) % 7;
    return opensOn + toMonday * DAY;
  }
  return t + tzOffset(t, tz);
}

/**
 * Map bars to chart times, dropping any whose chart time does not increase. That only
 * happens in the repeated hour when the display timezone leaves DST, which falls on a
 * weekend for forex and metals.
 */
export function toChartTimes<T extends { t: number }>(
  bars: T[],
  tf: string,
  tz: string,
  session: SessionMeta,
): { bars: T[]; times: number[] } {
  const outBars: T[] = [];
  const times: number[] = [];
  let last = -Infinity;
  for (const bar of bars) {
    const ct = chartTime(bar.t, tf, tz, session);
    if (ct > last) {
      outBars.push(bar);
      times.push(ct);
      last = ct;
    }
  }
  return { bars: outBars, times };
}

const WEEKDAYS = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"];
const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

const pad = (n: number) => String(n).padStart(2, "0");

/** Crosshair label for a chart time (already shifted, so read with UTC getters). */
export function formatChartTime(ct: number, tf: string): string {
  const d = new Date(ct * 1000);
  const date = `${WEEKDAYS[d.getUTCDay()]} ${d.getUTCDate()} ${MONTHS[d.getUTCMonth()]} '${String(
    d.getUTCFullYear(),
  ).slice(2)}`;
  if (tf === "1D" || tf === "1W") return date;
  const time = `${pad(d.getUTCHours())}:${pad(d.getUTCMinutes())}`;
  return tf.endsWith("s") ? `${date}  ${time}:${pad(d.getUTCSeconds())}` : `${date}  ${time}`;
}

/** "02:15:07" wall-clock time of `utc` in `tz`. */
export function formatClock(utc: number, tz: string): string {
  const d = new Date((utc + tzOffset(utc, tz)) * 1000);
  return `${pad(d.getUTCHours())}:${pad(d.getUTCMinutes())}:${pad(d.getUTCSeconds())}`;
}

/** "04:59", "1:02:03", or "3d 12h" for more than a day. */
export function formatCountdown(seconds: number): string {
  const s = Math.max(0, Math.floor(seconds));
  if (s >= 86400) return `${Math.floor(s / 86400)}d ${Math.floor((s % 86400) / 3600)}h`;
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  return h ? `${h}:${pad(m)}:${pad(s % 60)}` : `${pad(m)}:${pad(s % 60)}`;
}
