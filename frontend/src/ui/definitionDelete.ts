/**
 * Deleting one of your definitions, as data (no DOM): what to ask, what each answer of the
 * server means, and what to tell afterwards. `deleteDefinition.ts` draws the dialog and
 * makes the calls.
 *
 * The server moves the file (and its history) into `.deleted` inside your indicators
 * folder, so nothing is erased. A definition in use answers 409 with where it is used;
 * confirming removes its instances from every chart and saved layout first. That is two
 * steps on the server (instances, then the file), so a failure after a 5xx or no answer at
 * all is treated as "unknown state": the caller re-reads what it shows.
 */

import { ApiError, type DefinitionUsage, type DeletedDefinition } from "../api";
import { definitionActiveAlerts, definitionAlertIds } from "../alerts/confirmations";

export type DeletePhase = "confirm" | "blocked" | "unsupported" | "gone" | "failed";

export interface DeleteNext {
  phase: DeletePhase;
  text: string;
  /** Re-read the catalog, the charts and the usage before showing anything else. */
  refresh: boolean;
  /** `confirm` only: whether something uses the definition, and where. */
  inUse?: boolean;
  usage?: DefinitionUsage;
  /** `confirm` only: the indicator alerts that were shown. The delete names each of them; the server refuses one that is missing or extra. */
  alertIds?: string[];
}

function count(n: number, one: string, many = `${one}s`): string {
  return `${n} ${n === 1 ? one : many}`;
}

/** "EUR_USD (2 indicators)" per chart, "Gold H1 (1 indicator)" per saved layout. */
export function usageLines(usage: DefinitionUsage): { charts: string[]; layouts: string[] } {
  return {
    charts: usage.charts.map((c) => `${c.instrument} (${count(c.instances, "indicator", "indicators")})`),
    layouts: usage.layouts.map((l) => `${l.name} (${count(l.instances, "indicator", "indicators")})`),
  };
}

/** The button: what it will do, naming the running alerts it stops. */
export function confirmLabel(inUse: boolean, runningAlerts = 0): string {
  if (runningAlerts > 0) return `Delete and stop ${count(runningAlerts, "alert")}`;
  return inUse ? "Delete and remove from all" : "Delete";
}

/** What deleting does to a used definition, in words. */
function usedText(title: string, usage: DefinitionUsage): string {
  const alerts = usage.alerts ?? [];
  const running = definitionActiveAlerts(usage);
  const where = usage.charts.length || usage.layouts.length ? "removes it from all of these" : "";
  const stops = running ? `stops ${count(running, "active alert")} on it` : "";
  const parts = [where, stops].filter(Boolean).join(" and ");
  const idle = alerts.length && !running ? `the ${count(alerts.length, "alert")} on it ${alerts.length === 1 ? "is" : "are"} already stopped and stay in the Alerts panel` : "";
  const head = parts ? `Deleting it ${parts}` : "Deleting it only moves the file to the .deleted folder";
  const tail = parts ? "; the file moves to the .deleted folder." : ".";
  return `"${title}" is used. ${head}${idle ? `; ${idle}` : ""}${tail}`;
}

/** The first answer: ask, and say where it is used when it is. */
export function afterUsage(title: string, usage: DefinitionUsage): DeleteNext {
  return usage.in_use
    ? {
        phase: "confirm",
        inUse: true,
        usage,
        alertIds: definitionAlertIds(usage),
        refresh: false,
        text: usedText(title, usage),
      }
    : {
        phase: "confirm",
        inUse: false,
        usage,
        alertIds: definitionAlertIds(usage),
        refresh: false,
        text: `Delete "${title}"? The file moves to the .deleted folder inside your indicators folder; nothing is erased.`,
      };
}

function usageOf(detail: unknown): DefinitionUsage | null {
  const usage = (detail as { usage?: unknown } | null | undefined)?.usage as Partial<DefinitionUsage> | undefined;
  return usage && typeof usage.id === "string" && Array.isArray(usage.charts) && Array.isArray(usage.layouts)
    ? (usage as DefinitionUsage)
    : null;
}

/** What a failed call means. */
export function afterError(_title: string, err: unknown): DeleteNext {
  if (!(err instanceof ApiError)) {
    const why = err instanceof Error ? err.message : String(err);
    return {
      phase: "failed",
      text: `Could not delete: ${why}. Check the list: the file or the charts may have changed.`,
      refresh: true,
    };
  }
  switch (err.status) {
    case 403:
      return { phase: "blocked", text: `Deleting is not allowed here: ${err.message}`, refresh: false };
    case 404:
      // FastAPI's own 404 is "Not Found": the server has no such route. The server's own
      // 404 names the file: it is already gone.
      return err.message === "Not Found"
        ? { phase: "unsupported", text: "This server cannot delete indicators yet. Update OpenChart on the server first.", refresh: false }
        : { phase: "gone", text: err.message, refresh: true };
    case 409: {
      const usage = usageOf(err.detail);
      if (usage) {
        return { phase: "confirm", inUse: usage.in_use, usage, alertIds: definitionAlertIds(usage), text: err.message, refresh: false };
      }
      return { phase: "failed", text: `Could not delete: ${err.message}`, refresh: true };
    }
    default:
      return err.status >= 500
        ? {
            phase: "failed",
            text: `Could not delete: ${err.message}. Check the list: the file or the charts may have changed.`,
            refresh: true,
          }
        : { phase: "failed", text: `Could not delete: ${err.message}`, refresh: false };
  }
}

/** The toast after a delete that worked. */
export function afterDelete(title: string, done: DeletedDefinition): { title: string; body: string } {
  const removed = [
    done.removed.charts.length ? count(done.removed.charts.length, "chart", "charts") : "",
    done.removed.layouts.length ? count(done.removed.layouts.length, "saved layout", "saved layouts") : "",
  ].filter(Boolean);
  const tail = removed.length ? ` Removed from ${removed.join(" and ")}.` : "";
  return { title: "Deleted", body: `Moved "${title}" to ${done.moved_to}.${tail}` };
}
