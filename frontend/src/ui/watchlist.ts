/** Watchlist panel: every stored instrument with its live price and daily change. */

import { api, type Bar, type HistoryStatus, type Instrument, type Meta } from "../api";
import { h } from "./dom";
import { isDownloading, lineText, shortText } from "./download";

interface Row {
  inst: Instrument;
  el: HTMLButtonElement;
  last: HTMLElement;
  change: HTMLElement;
  badge: HTMLElement;
  prevClose?: number;
  price?: number;
}

export class Watchlist {
  private rows = new Map<string, Row>();
  private list: HTMLElement;

  constructor(
    panel: HTMLElement,
    private onSelect: (instrument: string) => void,
  ) {
    this.list = h("div", { class: "wl-list", role: "list" });
    panel.append(
      h("div", { class: "panel-head" }, h("span", { class: "title" }, "Watchlist")),
      h("div", { class: "wl-cols" }, h("span", {}, "Symbol"), h("span", {}, "Last"), h("span", {}, "Chg%")),
      this.list,
    );
  }

  setInstruments(instruments: Instrument[], current?: string): void {
    this.rows.clear();
    this.list.replaceChildren();
    if (!instruments.length) {
      this.list.append(h("div", { class: "panel-empty" }, "No instruments stored yet."));
      return;
    }
    for (const inst of instruments) {
      const last = h("span", { class: "num" }, "–");
      const change = h("span", { class: "num" }, "");
      const badge = h("span", { class: "dl-badge", hidden: true });
      const el = h(
        "button",
        { class: "wl-row", type: "button", role: "listitem" },
        h("span", { class: "sym" }, inst.instrument, h("small", {}, inst.display_name)),
        last,
        h("span", { class: "chg" }, change, badge),
      );
      el.addEventListener("click", () => this.onSelect(inst.instrument));
      this.rows.set(inst.instrument, { inst, el, last, change, badge });
      this.list.append(el);
    }
    this.setCurrent(current);
  }

  /** While the instrument's history downloads, a marker replaces its change column. */
  setDownload(instrument: string, history: HistoryStatus | null | undefined): void {
    const row = this.rows.get(instrument);
    if (!row) return;
    const on = isDownloading(history);
    row.badge.hidden = !on;
    row.change.hidden = on;
    if (on) {
      row.badge.textContent = shortText(history);
      row.badge.title = lineText(history);
    }
  }

  setCurrent(instrument?: string): void {
    for (const [name, row] of this.rows) row.el.classList.toggle("current", name === instrument);
  }

  /** Previous trading day's close, the base for the change column. */
  async loadPrevious(_meta: Meta): Promise<void> {
    await Promise.all(
      [...this.rows.values()].map(async (row) => {
        try {
          const days = await api.candles({ instrument: row.inst.instrument, timeframe: "1D", price: "mid", limit: 2 });
          const last = days[days.length - 1];
          if (!last) return;
          // If today's bar is still open, compare with yesterday; otherwise with the day before.
          const base = days.length > 1 ? days[days.length - 2] : undefined;
          row.prevClose = base?.c;
          this.setPrice(row, last.c);
        } catch {
          /* leave the row empty */
        }
      }),
    );
  }

  onTick(instrument: string, bid: number, ask: number): void {
    const row = this.rows.get(instrument);
    if (row) this.setPrice(row, (bid + ask) / 2);
  }

  onDaily(instrument: string, bar: Bar): void {
    const row = this.rows.get(instrument);
    if (row && row.prevClose === undefined) this.setPrice(row, bar.c);
  }

  private setPrice(row: Row, price: number): void {
    const before = row.price;
    row.price = price;
    row.last.textContent = price.toLocaleString("en-US", {
      minimumFractionDigits: row.inst.decimals,
      maximumFractionDigits: row.inst.decimals,
    });
    if (before !== undefined && price !== before) {
      const cls = price > before ? "flash-up" : "flash-down";
      row.last.classList.remove("flash-up", "flash-down");
      void row.last.offsetWidth; // restart the animation
      row.last.classList.add(cls);
    }
    if (row.prevClose) {
      const pct = ((price - row.prevClose) / row.prevClose) * 100;
      row.change.textContent = `${pct >= 0 ? "+" : ""}${pct.toFixed(2)}%`;
      row.change.style.color = pct >= 0 ? "var(--up)" : "var(--down)";
    }
  }
}
