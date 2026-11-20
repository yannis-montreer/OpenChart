import type { RetryStatus } from "./api";

/** "45 s", "4 min", "1 h 05 min": how long until the next attempt or how long it has lasted. */
export function shortDuration(seconds: number): string {
  const s = Math.max(0, Math.round(seconds));
  if (s < 60) return `${s} s`;
  const m = Math.round(s / 60);
  if (m < 60) return `${m} min`;
  return `${Math.floor(m / 60)} h ${String(m % 60).padStart(2, "0")} min`;
}

/** The connection dot while the broker rejects the credentials and the server retries
 * (`retry` in /api/status): the label, and the tooltip with the error and the next attempt. */
export function retryIndicator(error: string | null | undefined, retry: RetryStatus): { label: string; detail: string } {
  const next =
    retry.next_attempt_in === null ? "trying again now" : `next attempt in ${shortDuration(retry.next_attempt_in)}`;
  const giveUp = shortDuration(retry.gives_up_at - retry.since);
  return {
    label: "Feed down, retrying",
    detail:
      `${(error || "OANDA rejected the credentials").replace(/[.\s]+$/, "")}. The token may still be valid (OANDA answers this way ` +
      `during its own outages). The server keeps retrying: ${next} (retry ${retry.attempts}); ` +
      `it stops after ${giveUp}.`,
  };
}
