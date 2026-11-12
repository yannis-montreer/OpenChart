/**
 * The questions asked before something stops indicator alerts, as data with no DOM: what to
 * say, which alerts to list, which ids to send. The server decides (it refuses an apply or a
 * delete that does not name exactly the alerts it would stop); these texts say it in words.
 */

import { ApiError, type Alert, type ApplyPreview, type DefinitionUsage } from "../api";

export interface Prompt {
  /** False: nothing stops, so nothing is asked. */
  needed: boolean;
  title: string;
  text: string;
  lines: string[];
  confirm: string;
}

const count = (n: number, one: string, many = `${one}s`): string => `${n} ${n === 1 ? one : many}`;

export interface ApplyPrompt extends Prompt {
  /** What to send as `stop_alerts`: exactly the alerts the preview says would stop. */
  ids: string[];
  /** The alerts that start over (armed again, no fire on the first bar). */
  rearmed: string[];
}

export function layoutApplyPrompt(layoutName: string, preview: ApplyPreview): ApplyPrompt {
  const stopped = preview.alerts_stopped;
  const rearmed = preview.alerts_rearmed.map((a) => a.name);
  const title = `Apply “${layoutName}”?`;
  if (!stopped.length) return { needed: false, title, text: "", lines: [], confirm: "Apply", ids: [], rearmed };
  let text = `Applying this layout to ${preview.instrument} stops ${count(stopped.length, "indicator alert")}. They stay in the Alerts panel, stopped, with their settings.`;
  if (rearmed.length) {
    const one = rearmed.length === 1;
    text += ` ${count(rearmed.length, "other alert")} ${one ? "starts" : "start"} over and ${one ? "needs" : "need"} a fresh closed bar before ${one ? "it" : "they"} can fire.`;
  }
  return {
    needed: true,
    title,
    text,
    lines: stopped.map((a) => a.name),
    confirm: `Apply and stop ${count(stopped.length, "alert")}`,
    ids: stopped.map((a) => a.id),
    rearmed,
  };
}

/** The preview inside the apply's 409, or null for any other answer. */
export function previewOf(err: unknown): ApplyPreview | null {
  if (!(err instanceof ApiError) || err.status !== 409) return null;
  const p = (err.detail as { preview?: Partial<ApplyPreview> } | null | undefined)?.preview;
  return p && Array.isArray(p.alerts_stopped) && Array.isArray(p.alerts_rearmed) ? (p as ApplyPreview) : null;
}

/** Removing an instance stops its active alerts (stopped, triggered and expired ones stay as they are). */
export function removeInstancePrompt(title: string, alerts: readonly Alert[]): Prompt {
  const running = alerts.filter((a) => a.status === "active");
  if (!running.length) return { needed: false, title: "Remove indicator?", text: "", lines: [], confirm: "Remove" };
  const one = running.length === 1;
  return {
    needed: true,
    title: "Remove indicator?",
    text: `Removing ${title} stops ${count(running.length, "alert")}. ${one ? "It stays" : "They stay"} in the Alerts panel, stopped, with ${one ? "its" : "their"} settings.`,
    lines: running.map((a) => a.name),
    confirm: `Remove and stop ${count(running.length, "alert")}`,
  };
}

/** The alerts on a definition, one line each: "RSI under 30 (EUR_USD)", "old one (EUR_USD, stopped)". */
export function definitionAlertLines(usage: DefinitionUsage): string[] {
  return (usage.alerts ?? []).map((a) => `${a.name} (${a.instrument}${a.status === "active" ? "" : `, ${a.status}`})`);
}

/** Every alert the server listed: the delete names all of them. */
export function definitionAlertIds(usage: DefinitionUsage): string[] {
  return (usage.alerts ?? []).map((a) => a.id);
}

/** How many of them are running (the delete stops those). */
export function definitionActiveAlerts(usage: DefinitionUsage): number {
  return (usage.alerts ?? []).filter((a) => a.status === "active").length;
}
