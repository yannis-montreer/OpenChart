/**
 * A history download in progress, as the server reports it in `status` (per instrument):
 * newest first, step 1 hourly candles (as far back as the broker has them), step 2
 * minute candles (back to the configured years). Shown on the chart, in the watchlist
 * and in Settings.
 */

import type { HistoryStatus } from "../api";
import { h } from "./dom";

export function isDownloading(s: HistoryStatus | null | undefined): s is HistoryStatus {
  return !!s && (s.state === "pending" || s.state === "running" || s.state === "failed");
}

const day = (t: number) =>
  new Date(t * 1000).toLocaleDateString("en-GB", { day: "numeric", month: "short", year: "numeric", timeZone: "UTC" });

const pct = (s: HistoryStatus) => `${Math.floor((s.progress ?? 0) * 100)}%`;

/** A few characters for a list row: "↓ 37%". */
export function shortText(s: HistoryStatus): string {
  if (s.state === "pending") return "↓ waiting";
  if (s.state === "failed") return "↓ retrying";
  return s.phase === "h1" ? "↓ 1h" : `↓ ${pct(s)}`;
}

/** One line: what is being downloaded and how far back it is. */
export function lineText(s: HistoryStatus): string {
  if (s.state === "pending") return "History download waiting for another instrument to finish.";
  if (s.state === "failed") {
    return `History download interrupted (${s.error ?? "error"}). It retries every minute and continues where it stopped.`;
  }
  if (s.phase === "h1") {
    return `Step 1 of 2: hourly candles${s.oldest ? `, back to ${day(s.oldest)}` : ""}.`;
  }
  const reached = s.oldest ? `back to ${day(s.oldest)}` : "starting";
  const target = s.target ? ` of ${day(s.target)}` : "";
  return `Step 2 of 2: minute candles, ${reached}${target} (${pct(s)}). Charts of 1h and above are complete.`;
}

/** The panel on the chart while the instrument on screen downloads its history. */
export class DownloadPanel {
  readonly el: HTMLElement;
  private title = h("div", { class: "dl-title" });
  private line = h("div", { class: "dl-line" });
  private fill = h("div", { class: "dl-fill" });

  constructor() {
    this.el = h(
      "div",
      { class: "dl-panel", hidden: true, role: "status" },
      this.title,
      h("div", { class: "dl-bar" }, this.fill),
      this.line,
    );
  }

  update(instrument: string | undefined, s: HistoryStatus | null | undefined): void {
    if (!instrument || !isDownloading(s)) {
      this.el.hidden = true;
      return;
    }
    this.el.hidden = false;
    this.el.classList.toggle("failed", s.state === "failed");
    this.title.textContent = `Downloading ${instrument} history, newest first`;
    this.line.textContent = lineText(s);
    // Step 1 fills the first tenth of the bar (it is quick), step 2 the rest.
    const p = s.progress ?? 0;
    const overall = s.state === "pending" ? 0 : s.phase === "h1" ? 0.1 * p : 0.1 + 0.9 * p;
    this.fill.style.width = `${Math.max(2, Math.round(overall * 100))}%`;
  }
}
