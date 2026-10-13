/**
 * The alert state a line shows (SPEC section 10: "a drawing with an attached alert shows a
 * bell indicator and the alert state"): its bell in the style toolbar and on the chart.
 */

import type { Alert } from "../api";

/** `active`: at least one alert watches the line. `inactive`: all are stopped, expired or done. */
export type LineAlertState = "active" | "inactive";

export function lineAlertState(alerts: readonly Pick<Alert, "status">[]): LineAlertState | null {
  if (!alerts.length) return null;
  return alerts.some((a) => a.status === "active") ? "active" : "inactive";
}

/** Tooltip for the line's bell. */
export function lineAlertTitle(alerts: readonly Pick<Alert, "status">[]): string {
  const state = lineAlertState(alerts);
  if (state === null) return "Add alert on this line (Alt+A)";
  const what = alerts.length === 1 ? "alert" : `${alerts.length} alerts`;
  if (state === "active") return `Edit ${what} on this line (Alt+A)`;
  const why = alerts.length === 1 ? ` (${alerts[0].status})` : " (none active)";
  return `Edit ${what} on this line${why} (Alt+A)`;
}

/** The point `fraction` of the way along a screen polyline (where the line's bell sits). */
export function pointAlong(path: readonly [number, number][], fraction: number): [number, number] | null {
  if (!path.length) return null;
  const lengths = path.slice(1).map(([x, y], i) => Math.hypot(x - path[i][0], y - path[i][1]));
  let rest = lengths.reduce((a, b) => a + b, 0) * fraction;
  for (let i = 0; i < lengths.length; i++) {
    if (rest <= lengths[i] && lengths[i] > 0) {
      const k = rest / lengths[i];
      const [ax, ay] = path[i];
      const [bx, by] = path[i + 1];
      return [ax + (bx - ax) * k, ay + (by - ay) * k];
    }
    rest -= lengths[i];
  }
  return path[path.length - 1];
}
