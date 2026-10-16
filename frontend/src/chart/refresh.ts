/**
 * When an indicator's values must be fetched again in full, rather than only for the last bars.
 *
 * The once-a-second refresh asks for the last two bars. That is enough while the file does not
 * change, but an edited file gives other values on every bar: the answer names the file's version
 * (`checks.version`, custom indicators), and a new one means a full load. A failed indicator is
 * tried again in full every few seconds, so a fixed script comes back without a reload.
 */

/** How often an indicator in error is asked again. */
export const RETRY_MS = 5000;

/** The answer is for another version of the file than the values held. */
export function versionChanged(held: string | undefined, answered: string | undefined): boolean {
  return held !== undefined && answered !== undefined && held !== answered;
}

export type Refresh = "tail" | "reload" | "wait";

export function nextRefresh(state: { error: boolean; lastTry: number }, now: number): Refresh {
  if (!state.error) return "tail";
  return now - state.lastTry >= RETRY_MS ? "reload" : "wait";
}
