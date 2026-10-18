/**
 * Keeps the chart's indicator list in step with the server (CI-3, docs/api.md "Indicators
 * per chart"). The list lives on the server, one per chart; this reads it when a chart is
 * opened, takes the browser's old list into a chart that has none, writes every change as
 * the whole list, and follows what other browsers do. No DOM: the app passes what to show
 * and where the words go.
 *
 * Rules, each with a test:
 * - one write at a time, the newest list per chart; a write carries the revision it was
 *   based on, and a 409 means the server's list wins (it is shown, with a note);
 * - a chart's own write coming back as a websocket message is not shown again, even when the
 *   message arrives before the answer;
 * - changes made while the list on screen is another chart's, or was never loaded, are not
 *   written;
 * - the old browser list is offered once per chart, never empty, never over a list.
 */

import { ApiError, type ChartIndicatorIn, type ChartIndicators, type IndicatorEntry, type Layout, type SkippedEntry } from "../api";
import { askToImport, legacyDone, legacyEntries, resetItems, toServer, wsAction, type SyncEvent } from "./chartList";

export interface SyncApi {
  get(source: string, instrument: string): Promise<ChartIndicators>;
  put(source: string, instrument: string, body: { indicators: ChartIndicatorIn[]; revision?: number }): Promise<ChartIndicators>;
  import(source: string, instrument: string, indicators: unknown[]): Promise<ChartIndicators & { skipped: SkippedEntry[] }>;
  apply(layoutId: string, instrument: string): Promise<{ layout: Layout; chart: ChartIndicators; skipped: SkippedEntry[] }>;
}

export type ShowWhy = "open" | "remote" | "conflict" | "apply" | "dropped";

export interface SyncHooks {
  /** Replace the legend with the server's list. */
  show(chart: ChartIndicators, why: ShowWhy): void;
  /** The list on screen, in order, as it is saved: without the instances whose definition is gone (the server refuses those). */
  entries(): IndicatorEntry[];
  /** The ids of the instances `entries` left out. */
  missing(): string[];
  notify(event: SyncEvent): void;
  /** The browser's old list (before CI-3), as stored. */
  legacy(): unknown[];
  /** Forget the old list for good. */
  dropLegacy(): void;
  titleOf(id: string): string;
  /** Every chart that is enabled (its list is needed before the old list can go). */
  enabled(): Array<{ source: string; instrument: string }>;
}

interface Key {
  source: string;
  instrument: string;
}

const keyOf = (k: Key) => `${k.source}/${k.instrument}`;

/** The chart a 409 carries. */
function chartOf(err: unknown): ChartIndicators | null {
  if (!(err instanceof ApiError) || err.status !== 409) return null;
  const chart = (err.detail as { chart?: ChartIndicators } | null | undefined)?.chart;
  return chart && Array.isArray(chart.indicators) && typeof chart.revision === "number" ? chart : null;
}

function messageOf(err: unknown): string {
  return err instanceof Error ? err.message : String(err);
}

/** What the legend shows besides the list itself: each instance and the state of its definition. */
function statusKey(chart: ChartIndicators): string {
  return chart.indicators.map((i) => `${i.uid}:${i.status}`).join("|");
}

export class ChartSync {
  private current: Key | null = null;
  /** The list on screen is the current chart's own (it was read). */
  private loaded = false;
  /** A read is under way: the list on screen may still be the previous chart's. */
  private loading = false;
  private token = 0;
  private revisions = new Map<string, number>();
  /** The statuses of the list last shown, per chart. */
  private statuses = new Map<string, string>();
  private queue = new Map<string, { key: Key; entries: IndicatorEntry[]; gone: string[] }>();
  private running: Promise<void> | null = null;
  private busy = new Map<string, number>();
  private deferred: ChartIndicators[] = [];
  private tried = new Set<string>();
  private announced = new Set<string>();
  private legacyChecked = false;

  constructor(
    private api: SyncApi,
    private hooks: SyncHooks,
  ) {}

  /** Read a chart's list and show it; the old browser list goes in first when the chart has none. */
  async open(source: string, instrument: string): Promise<void> {
    const key = { source, instrument };
    const mine = ++this.token;
    this.current = key;
    this.loaded = false;
    this.loading = true;
    let imported = false;
    await this.guard(keyOf(key), async () => {
      try {
        await this.settled();
        if (mine !== this.token) return;
        let chart = await this.api.get(source, instrument);
        if (mine !== this.token) return;
        const taken = await this.importInto(key, chart);
        if (mine !== this.token) return;
        imported = taken !== chart;
        chart = taken;
        this.take(chart, "open");
        this.loaded = true;
      } catch {
        // The list stays unloaded: changes are refused until a later open works.
      } finally {
        if (mine === this.token) this.loading = false;
      }
    });
    if (!this.legacyChecked || imported) void this.checkLegacy();
  }

  /** Is this the chart whose list is on screen (or being read)? */
  isCurrent(source: string, instrument: string): boolean {
    return this.current?.source === source && this.current.instrument === instrument;
  }

  /** The user changed the list on screen: write it. */
  changed(): void {
    const key = this.current;
    if (!key || this.loading) return; // what is on screen is not this chart's list (yet)
    if (!this.loaded) {
      this.hooks.notify({ type: "unloaded", instrument: key.instrument });
      return;
    }
    this.queue.set(keyOf(key), { key, entries: this.hooks.entries(), gone: this.hooks.missing() });
    this.pump();
  }

  /** Every write that was asked for has been answered. */
  settled(): Promise<void> {
    return this.running ?? Promise.resolve();
  }

  /** A `chart_indicators_changed` message. */
  receive(msg: ChartIndicators): void {
    const k = keyOf(msg);
    if (this.busy.get(k)) {
      this.deferred.push(msg); // maybe our own write on its way back: decide once the answer is in
      return;
    }
    const known = this.revisions.get(k) ?? 0;
    switch (wsAction(msg, this.current, known)) {
      case "take":
        this.queue.delete(k);
        this.take(msg, "remote");
        this.hooks.notify({ type: "remote", instrument: msg.instrument });
        break;
      case "note":
        if (msg.revision > known) {
          this.revisions.set(k, msg.revision);
          this.queue.delete(k); // an unsent edit was based on an older list: the server's wins
        }
        break;
      case "ignore":
        break;
    }
  }

  /**
   * A definition of yours was saved or deleted (here or in another browser): a file may now fail
   * to load, or load again, with the list itself unchanged (the same revision). Read the list
   * and show it when a status differs; a newer list is taken as any change from elsewhere is.
   */
  async refreshStatuses(): Promise<void> {
    const key = this.current;
    if (!key || !this.loaded || this.loading) return;
    const mine = this.token;
    const k = keyOf(key);
    await this.guard(k, async () => {
      await this.settled();
      try {
        const chart = await this.api.get(key.source, key.instrument);
        if (mine !== this.token) return;
        const known = this.revisions.get(k) ?? 0;
        if (chart.revision > known) {
          this.take(chart, "remote");
          this.hooks.notify({ type: "remote", instrument: key.instrument });
        } else if (chart.revision === known && statusKey(chart) !== this.statuses.get(k)) {
          this.take(chart, "open");
        }
      } catch {
        // unreachable: the statuses are read again with the next change
      }
    });
  }

  /** After a reconnect: changes made while away. */
  async resync(): Promise<void> {
    const key = this.current;
    if (!key || !this.loaded || this.loading) return;
    const mine = this.token;
    await this.guard(keyOf(key), async () => {
      await this.settled();
      try {
        const chart = await this.api.get(key.source, key.instrument);
        if (mine !== this.token) return;
        if (chart.revision > (this.revisions.get(keyOf(key)) ?? 0)) {
          this.take(chart, "remote");
          this.hooks.notify({ type: "remote", instrument: key.instrument });
        }
      } catch {
        // still away: the next reconnect tries again
      }
    });
  }

  /** Put a saved layout's list on a chart (replacing the chart's own). */
  async applyLayout(layoutId: string, layoutName: string, source: string, instrument: string): Promise<void> {
    const key = { source, instrument };
    const k = keyOf(key);
    const mine = ++this.token;
    this.current = key;
    this.loaded = false;
    this.loading = true;
    this.queue.delete(k); // the layout replaces whatever was waiting
    let failed = false;
    await this.guard(k, async () => {
      try {
        await this.settled();
        if (mine !== this.token) return;
        const done = await this.api.apply(layoutId, instrument);
        if (mine !== this.token) return;
        this.take(done.chart, "apply");
        this.loaded = true;
        if (done.skipped.length) this.hooks.notify({ type: "applied", instrument, layout: layoutName, skipped: done.skipped });
      } catch (err) {
        failed = true;
        this.hooks.notify({ type: "failed", instrument, message: messageOf(err) });
      } finally {
        if (mine === this.token) this.loading = false;
      }
    });
    if (failed && mine === this.token) await this.open(source, instrument);
  }

  // Writing

  private pump(): void {
    if (this.running) return;
    this.running = (async () => {
      try {
        while (this.queue.size) {
          const [k, job] = this.queue.entries().next().value as [string, { key: Key; entries: IndicatorEntry[]; gone: string[] }];
          this.queue.delete(k);
          await this.guard(k, () => this.send(k, job.key, job.entries, job.gone));
        }
      } finally {
        this.running = null;
      }
    })();
  }

  private async send(k: string, key: Key, entries: IndicatorEntry[], gone: string[]): Promise<void> {
    try {
      const got = await this.api.put(key.source, key.instrument, { indicators: toServer(entries), revision: this.revisions.get(k) });
      this.revisions.set(k, got.revision);
      this.announceResets(got);
      this.hooks.notify({ type: "saved" });
      if (gone.length && this.current && keyOf(this.current) === k) {
        // What was left out is off the server's list now: show the list as it is.
        this.take(got, "dropped");
        this.hooks.notify({ type: "dropped", instrument: key.instrument, ids: gone });
      }
    } catch (err) {
      const chart = chartOf(err);
      if (chart) {
        this.revisions.set(k, chart.revision);
        this.queue.delete(k); // an edit queued behind this one was based on the same older list
        if (this.current && keyOf(this.current) === k) {
          this.take(chart, "conflict");
          this.hooks.notify({ type: "conflict", instrument: key.instrument });
        }
        return;
      }
      this.hooks.notify({ type: "failed", instrument: key.instrument, message: messageOf(err) });
    }
  }

  // Reading

  private async importInto(key: Key, chart: ChartIndicators): Promise<ChartIndicators> {
    const k = keyOf(key);
    const legacy = legacyEntries(this.hooks.legacy());
    if (!askToImport(legacy, chart) || this.tried.has(k)) return chart;
    this.tried.add(k); // once per chart and page: the server takes it once anyway
    try {
      const done = await this.api.import(key.source, key.instrument, legacy);
      this.hooks.notify({ type: "imported", instrument: key.instrument, count: done.indicators.length, skipped: done.skipped });
      return done;
    } catch (err) {
      return chartOf(err) ?? chart; // another browser was first: its list is the chart's
    }
  }

  private take(chart: ChartIndicators, why: ShowWhy): void {
    this.revisions.set(keyOf(chart), chart.revision);
    this.statuses.set(keyOf(chart), statusKey(chart));
    this.hooks.show(chart, why);
    this.announceResets(chart);
  }

  /** Once per revision: the inputs that went back to their defaults. */
  private announceResets(chart: ChartIndicators): void {
    const items = resetItems(chart, this.hooks.titleOf);
    const tag = `${keyOf(chart)}@${chart.revision}`;
    if (!items.length || this.announced.has(tag)) return;
    this.announced.add(tag);
    this.hooks.notify({ type: "reset", instrument: chart.instrument, items });
  }

  /** The old browser list can go when no enabled chart is left without a list of its own. */
  private async checkLegacy(): Promise<void> {
    this.legacyChecked = true;
    if (!legacyEntries(this.hooks.legacy()).length) return;
    const revisions = await Promise.all(
      this.hooks.enabled().map(async (c) => {
        const known = this.revisions.get(keyOf(c)) ?? 0;
        if (known > 0) return known;
        try {
          return (await this.api.get(c.source, c.instrument)).revision;
        } catch {
          return 0;
        }
      }),
    );
    if (legacyDone(revisions)) this.hooks.dropLegacy();
  }

  /** While a call for a chart is under way, messages for it wait; then they are looked at. */
  private async guard(k: string, fn: () => Promise<void>): Promise<void> {
    this.busy.set(k, (this.busy.get(k) ?? 0) + 1);
    try {
      await fn();
    } finally {
      const left = (this.busy.get(k) ?? 1) - 1;
      if (left > 0) this.busy.set(k, left);
      else {
        this.busy.delete(k);
        const mine = this.deferred.filter((m) => keyOf(m) === k);
        this.deferred = this.deferred.filter((m) => keyOf(m) !== k);
        for (const m of mine) this.receive(m);
      }
    }
  }
}
