import { describe, expect, it, vi } from "vitest";

import { apiErrorFrom, type ChartIndicator, type ChartIndicatorIn, type ChartIndicators, type IndicatorEntry, type SkippedEntry } from "../api";
import type { SyncEvent } from "./chartList";
import { ChartSync, type SyncApi, type SyncHooks } from "./chartSync";

const SRC = "oanda";
const A = "EUR_USD";
const B = "XAU_USD";
const key = (s: string, i: string) => `${s}/${i}`;

const inst = (id: string, over: Partial<ChartIndicator> = {}): ChartIndicator => ({
  uid: `u-${id}`,
  kind: "builtin",
  id,
  params: { length: 9 },
  visible: true,
  price_labels: true,
  legend_values: true,
  status: "ok",
  reset: [],
  ...over,
});

const entry = (id: string, over: Partial<IndicatorEntry> = {}): IndicatorEntry => ({
  uid: `u-${id}`,
  kind: "builtin",
  id,
  params: { length: 9 },
  visible: true,
  price_labels: true,
  legend_values: true,
  ...over,
});

/** A server in memory: revisions, the 409 rules of docs/api.md, and a log of calls. */
function fakeServer(start: Record<string, { revision: number; indicators: ChartIndicator[] }> = {}) {
  const charts = new Map(Object.entries(start));
  const calls: string[] = [];
  const gates: Array<() => void> = [];
  let hold = false;
  const chartOf = (s: string, i: string): ChartIndicators => {
    const c = charts.get(key(s, i)) ?? { revision: 0, indicators: [] };
    return { source: s, instrument: i, revision: c.revision, indicators: structuredClone(c.indicators) };
  };
  const wait = () => (hold ? new Promise<void>((r) => gates.push(r)) : Promise.resolve());
  const conflict = (s: string, i: string, message: string) => {
    const err = apiErrorFrom(409, "Conflict", { detail: { message, chart: chartOf(s, i) } });
    return err;
  };
  const api: SyncApi & { fail?: (what: string) => Error | null } = {
    get: vi.fn(async (s: string, i: string) => {
      calls.push(`get ${i}`);
      const answer = chartOf(s, i); // what the server holds when the request reaches it
      await wait();
      if (api.fail?.("get")) throw api.fail("get");
      return answer;
    }),
    put: vi.fn(async (s: string, i: string, body: { indicators: ChartIndicatorIn[]; revision?: number }) => {
      calls.push(`put ${i} rev=${body.revision} ids=${body.indicators.map((e) => e.id).join(",")}`);
      await wait();
      if (api.fail?.("put")) throw api.fail("put");
      const cur = charts.get(key(s, i)) ?? { revision: 0, indicators: [] };
      if (body.revision !== undefined && body.revision !== cur.revision) throw conflict(s, i, "The chart moved on");
      const next = { revision: cur.revision + 1, indicators: body.indicators.map((e) => inst(e.id, { uid: e.uid ?? `n-${e.id}`, kind: e.kind ?? "builtin", params: e.params })) };
      charts.set(key(s, i), next);
      return chartOf(s, i);
    }),
    import: vi.fn(async (s: string, i: string, list: unknown[]) => {
      calls.push(`import ${i} n=${list.length}`);
      await wait();
      if (api.fail?.("import")) throw api.fail("import");
      const cur = charts.get(key(s, i)) ?? { revision: 0, indicators: [] };
      if (cur.revision !== 0) throw conflict(s, i, "Already set");
      const next = { revision: 1, indicators: list.map((e) => inst((e as { id: string }).id)) };
      charts.set(key(s, i), next);
      return { ...chartOf(s, i), skipped: [] as SkippedEntry[] };
    }),
    apply: vi.fn(async (_id: string, i: string) => {
      calls.push(`apply ${i}`);
      await wait();
      if (api.fail?.("apply")) throw api.fail("apply");
      const cur = charts.get(key(SRC, i)) ?? { revision: 0, indicators: [] };
      charts.set(key(SRC, i), { revision: cur.revision + 1, indicators: [inst("rsi")] });
      const layout = { id: "L1", name: "Gold H1", updated_at: "", default: false, layout: {} } as never;
      return { layout, chart: chartOf(SRC, i), skipped: [{ id: "gone", kind: "custom", reason: "no indicator with this id" }] as SkippedEntry[] };
    }),
  };
  return {
    api,
    calls,
    charts,
    set: (s: string, i: string, revision: number, indicators: ChartIndicator[]) => charts.set(key(s, i), { revision, indicators }),
    hold: () => (hold = true),
    release: () => {
      hold = false;
      gates.splice(0).forEach((r) => r());
    },
  };
}

function fakeHooks(over: Partial<SyncHooks> = {}) {
  const shown: Array<{ chart: ChartIndicators; why: string }> = [];
  const events: SyncEvent[] = [];
  let list: IndicatorEntry[] = [];
  const hooks: SyncHooks = {
    show: (chart, why) => {
      shown.push({ chart, why });
      list = chart.indicators.map((c) => entry(c.id, { uid: c.uid, kind: c.kind }));
    },
    entries: () => structuredClone(list),
    notify: (e) => events.push(e),
    legacy: () => [],
    dropLegacy: vi.fn(),
    titleOf: (id) => id.toUpperCase(),
    enabled: () => [{ source: SRC, instrument: A }],
    missing: () => [],
    ...over,
  };
  return { hooks, shown, events, setList: (l: IndicatorEntry[]) => (list = l), getList: () => list };
}

const flush = () => new Promise<void>((r) => setTimeout(r, 0));

describe("opening a chart", () => {
  it("reads its list and shows it", async () => {
    const s = fakeServer({ [key(SRC, A)]: { revision: 4, indicators: [inst("ema")] } });
    const h = fakeHooks();
    const sync = new ChartSync(s.api, h.hooks);
    await sync.open(SRC, A);
    expect(h.shown.map((x) => [x.why, x.chart.revision, x.chart.indicators.map((i) => i.id)])).toEqual([["open", 4, ["ema"]]]);
  });

  it("a chart never set with an old browser list gets that list once, and says so", async () => {
    const s = fakeServer();
    const h = fakeHooks({ legacy: () => [entry("ema"), entry("rsi")] });
    const sync = new ChartSync(s.api, h.hooks);
    await sync.open(SRC, A);
    expect(s.calls).toEqual([`get ${A}`, `import ${A} n=2`]);
    expect(h.shown[0].chart.indicators.map((i) => i.id)).toEqual(["ema", "rsi"]);
    expect(h.events).toContainEqual({ type: "imported", instrument: A, count: 2, skipped: [] });
  });

  it("never imports an empty list", async () => {
    const s = fakeServer();
    const h = fakeHooks({ legacy: () => [] });
    await new ChartSync(s.api, h.hooks).open(SRC, A);
    expect(s.calls).toEqual([`get ${A}`]);
  });

  it("never imports into a chart that has a list", async () => {
    const s = fakeServer({ [key(SRC, A)]: { revision: 2, indicators: [] } });
    const h = fakeHooks({ legacy: () => [entry("ema")] });
    await new ChartSync(s.api, h.hooks).open(SRC, A);
    expect(s.calls).toEqual([`get ${A}`]);
    expect(h.events.some((e) => e.type === "imported")).toBe(false);
  });

  it("a 409 on the import (another browser was first) adopts the server's list, without a toast about copying", async () => {
    const s = fakeServer();
    const real = s.api.get;
    s.api.get = vi.fn(async (a: string, b: string) => {
      const got = await real(a, b);
      s.set(SRC, A, 1, [inst("macd")]); // the other browser imports between our read and our import
      return got;
    });
    const h = fakeHooks({ legacy: () => [entry("ema")] });
    await new ChartSync(s.api, h.hooks).open(SRC, A);
    expect(h.shown.at(-1)?.chart.indicators.map((i) => i.id)).toEqual(["macd"]);
    expect(h.events.some((e) => e.type === "imported")).toBe(false);
  });

  it("an import that fails for another reason leaves the empty list and tries once only", async () => {
    const s = fakeServer();
    let fail = true;
    s.api.fail = (what) => (what === "import" && fail ? apiErrorFrom(500, "Server Error", {}) : null);
    const h = fakeHooks({ legacy: () => [entry("ema")] });
    const sync = new ChartSync(s.api, h.hooks);
    await sync.open(SRC, A);
    expect(h.shown.at(-1)?.chart.indicators).toEqual([]);
    fail = false;
    await sync.open(SRC, A);
    expect(s.api.import).toHaveBeenCalledTimes(1);
  });

  it("the legacy key is dropped once every enabled chart has a list", async () => {
    const s = fakeServer({ [key(SRC, B)]: { revision: 2, indicators: [] } });
    const drop = vi.fn();
    const h = fakeHooks({
      legacy: () => [entry("ema")],
      enabled: () => [
        { source: SRC, instrument: A },
        { source: SRC, instrument: B },
      ],
      dropLegacy: drop,
    });
    await new ChartSync(s.api, h.hooks).open(SRC, A);
    await flush();
    expect(drop).toHaveBeenCalledTimes(1);
  });

  it("the legacy key stays while an enabled chart has no list yet", async () => {
    const s = fakeServer();
    const drop = vi.fn();
    const h = fakeHooks({
      legacy: () => [entry("ema")],
      enabled: () => [
        { source: SRC, instrument: A },
        { source: SRC, instrument: B },
      ],
      dropLegacy: drop,
    });
    await new ChartSync(s.api, h.hooks).open(SRC, A);
    await flush();
    expect(drop).not.toHaveBeenCalled();
  });

  it("the legacy key stays when a chart cannot be read", async () => {
    const s = fakeServer({ [key(SRC, A)]: { revision: 1, indicators: [] } });
    s.api.fail = () => null;
    const real = s.api.get;
    s.api.get = vi.fn(async (a: string, b: string) => {
      if (b === B) throw new Error("offline");
      return real(a, b);
    });
    const drop = vi.fn();
    const h = fakeHooks({
      legacy: () => [entry("ema")],
      enabled: () => [
        { source: SRC, instrument: A },
        { source: SRC, instrument: B },
      ],
      dropLegacy: drop,
    });
    await new ChartSync(s.api, h.hooks).open(SRC, A);
    await flush();
    expect(drop).not.toHaveBeenCalled();
  });

  it("says once per revision which inputs went back to their defaults", async () => {
    const s = fakeServer({ [key(SRC, A)]: { revision: 2, indicators: [inst("ema", { reset: ["length"] })] } });
    const h = fakeHooks();
    const sync = new ChartSync(s.api, h.hooks);
    await sync.open(SRC, A);
    await sync.open(SRC, A);
    expect(h.events.filter((e) => e.type === "reset")).toEqual([{ type: "reset", instrument: A, items: ["EMA: length"] }]);
  });

  it("an answer that arrives after the user moved to another chart is not shown", async () => {
    const s = fakeServer({ [key(SRC, A)]: { revision: 1, indicators: [inst("ema")] }, [key(SRC, B)]: { revision: 1, indicators: [inst("rsi")] } });
    const h = fakeHooks();
    const sync = new ChartSync(s.api, h.hooks);
    s.hold();
    const first = sync.open(SRC, A);
    const second = sync.open(SRC, B);
    s.release();
    await Promise.all([first, second]);
    expect(h.shown.map((x) => x.chart.instrument)).toEqual([B]);
    expect(s.calls).toEqual([`get ${B}`]); // the chart that was left is not even asked for
  });

  it("an old browser list is not imported into a chart the user has already left", async () => {
    const s = fakeServer({ [key(SRC, B)]: { revision: 1, indicators: [inst("rsi")] } });
    const h = fakeHooks({ legacy: () => [entry("ema")] });
    const sync = new ChartSync(s.api, h.hooks);
    // A is asked for first; B replaces it while A's answer is on the way.
    const real = s.api.get;
    let release!: () => void;
    s.api.get = vi.fn(async (a: string, b: string) => {
      const got = await real(a, b);
      if (b === A) await new Promise<void>((r) => (release = r));
      return got;
    });
    const first = sync.open(SRC, A);
    await flush();
    const second = sync.open(SRC, B);
    await second;
    release();
    await first;
    expect(s.api.import).not.toHaveBeenCalled();
  });

  it("an import that finishes after the user moved on is not shown", async () => {
    const s = fakeServer({ [key(SRC, B)]: { revision: 1, indicators: [inst("rsi")] } });
    const h = fakeHooks({ legacy: () => [entry("ema")] });
    const sync = new ChartSync(s.api, h.hooks);
    const real = s.api.import;
    let release!: () => void;
    s.api.import = vi.fn(async (a: string, b: string, list: unknown[]) => {
      const got = await real(a, b, list);
      await new Promise<void>((r) => (release = r));
      return got;
    });
    const first = sync.open(SRC, A);
    await flush();
    await sync.open(SRC, B);
    release();
    await first;
    expect(h.shown.map((x) => x.chart.instrument)).toEqual([B]);
  });

  it("reads a chart only after the writes for it have been answered", async () => {
    const s = fakeServer({ [key(SRC, A)]: { revision: 3, indicators: [inst("ema")] } });
    const h = fakeHooks();
    const sync = new ChartSync(s.api, h.hooks);
    await sync.open(SRC, A);
    s.hold();
    h.setList([entry("ema"), entry("rsi")]);
    sync.changed();
    const again = sync.open(SRC, A);
    s.release();
    await again;
    expect(h.shown.at(-1)?.chart.revision).toBe(4);
    expect(h.shown.at(-1)?.chart.indicators.map((i) => i.id)).toEqual(["ema", "rsi"]);
  });

  it("a failed read is reported once changes are tried, and nothing is written", async () => {
    const s = fakeServer();
    s.api.fail = (what) => (what === "get" ? new Error("offline") : null);
    const h = fakeHooks();
    const sync = new ChartSync(s.api, h.hooks);
    await sync.open(SRC, A);
    sync.changed();
    await sync.settled();
    expect(s.api.put).not.toHaveBeenCalled();
    expect(h.events).toContainEqual({ type: "unloaded", instrument: A });
  });
});

describe("saving", () => {
  async function opened(revision = 3) {
    const s = fakeServer({ [key(SRC, A)]: { revision, indicators: [inst("ema")] } });
    const h = fakeHooks();
    const sync = new ChartSync(s.api, h.hooks);
    await sync.open(SRC, A);
    return { s, h, sync };
  }

  it("sends the whole list with the revision it was based on, and moves on to the new one", async () => {
    const { s, h, sync } = await opened(3);
    h.setList([entry("ema"), entry("rsi")]);
    sync.changed();
    await sync.settled();
    expect(s.calls.at(-1)).toBe(`put ${A} rev=3 ids=ema,rsi`);
    h.setList([entry("ema")]);
    sync.changed();
    await sync.settled();
    expect(s.calls.at(-1)).toBe(`put ${A} rev=4 ids=ema`);
    expect(h.events.filter((e) => e.type === "saved")).toHaveLength(2);
  });

  it("changes made while a save is under way go out as one save afterwards", async () => {
    const { s, h, sync } = await opened(3);
    s.hold();
    h.setList([entry("ema"), entry("rsi")]);
    sync.changed();
    h.setList([entry("ema"), entry("rsi"), entry("macd")]);
    sync.changed();
    h.setList([entry("macd")]);
    sync.changed();
    s.release();
    await sync.settled();
    expect(s.calls.filter((c) => c.startsWith("put"))).toEqual([`put ${A} rev=3 ids=ema,rsi`, `put ${A} rev=4 ids=macd`]);
  });

  it("a 409 shows the server's list and says the last change was not saved", async () => {
    const { s, h, sync } = await opened(3);
    s.set(SRC, A, 4, [inst("macd")]);
    h.setList([entry("ema"), entry("rsi")]);
    sync.changed();
    await sync.settled();
    expect(h.shown.at(-1)).toMatchObject({ why: "conflict" });
    expect(h.shown.at(-1)?.chart.indicators.map((i) => i.id)).toEqual(["macd"]);
    expect(h.events).toContainEqual({ type: "conflict", instrument: A });
    h.setList([entry("macd"), entry("ema")]);
    sync.changed();
    await sync.settled();
    expect(s.calls.at(-1)).toBe(`put ${A} rev=4 ids=macd,ema`);
  });

  it("another failure is reported and the next change tries again", async () => {
    const { s, h, sync } = await opened(3);
    let down = true;
    s.api.fail = (what) => (what === "put" && down ? new Error("Server down") : null);
    h.setList([entry("ema"), entry("rsi")]);
    sync.changed();
    await sync.settled();
    expect(h.events).toContainEqual({ type: "failed", instrument: A, message: "Server down" });
    down = false;
    h.setList([entry("ema"), entry("rsi"), entry("macd")]);
    sync.changed();
    await sync.settled();
    expect(s.calls.at(-1)).toBe(`put ${A} rev=3 ids=ema,rsi,macd`);
  });

  it("an edit made before the new chart's list has arrived is not written to it", async () => {
    const s = fakeServer({ [key(SRC, A)]: { revision: 1, indicators: [inst("ema")] }, [key(SRC, B)]: { revision: 5, indicators: [inst("rsi")] } });
    const h = fakeHooks();
    const sync = new ChartSync(s.api, h.hooks);
    await sync.open(SRC, A);
    s.hold();
    const opening = sync.open(SRC, B);
    h.setList([entry("ema"), entry("macd")]); // still A's list on screen
    sync.changed();
    s.release();
    await opening;
    await sync.settled();
    expect(s.api.put).not.toHaveBeenCalled();
    expect(h.events.some((e) => e.type === "unloaded")).toBe(false); // it was only not loaded yet
  });

  it("an edit made just before switching charts is saved to the chart it was made on", async () => {
    const s = fakeServer({ [key(SRC, A)]: { revision: 1, indicators: [inst("ema")] }, [key(SRC, B)]: { revision: 5, indicators: [inst("rsi")] } });
    const h = fakeHooks();
    const sync = new ChartSync(s.api, h.hooks);
    await sync.open(SRC, A);
    h.setList([entry("ema"), entry("macd")]);
    s.hold();
    sync.changed();
    const opening = sync.open(SRC, B);
    s.release();
    await opening;
    await sync.settled();
    expect(s.calls).toContain(`put ${A} rev=1 ids=ema,macd`);
    expect(h.shown.at(-1)?.chart.instrument).toBe(B);
  });

  it("a placeholder (a file that does not load) is sent back as it was", async () => {
    const s = fakeServer({ [key(SRC, A)]: { revision: 1, indicators: [inst("mine", { kind: "custom", status: "error", params: { n: 3 } })] } });
    const h = fakeHooks();
    h.hooks.show = (chart, why) => {
      h.shown.push({ chart, why });
      h.setList(chart.indicators.map((c) => entry(c.id, { uid: c.uid, kind: c.kind, params: c.params })));
    };
    const sync = new ChartSync(s.api, h.hooks);
    await sync.open(SRC, A);
    h.setList([...h.getList(), entry("ema")]);
    sync.changed();
    await sync.settled();
    expect(s.calls.at(-1)).toBe(`put ${A} rev=1 ids=mine,ema`);
    expect(s.charts.get(key(SRC, A))?.indicators[0]).toMatchObject({ id: "mine", kind: "custom", params: { n: 3 } });
  });

  it("an instance whose definition is gone is left out of the save (the server refuses it), and the list is read back with a note", async () => {
    const s = fakeServer({ [key(SRC, A)]: { revision: 3, indicators: [inst("ema")] } });
    const h = fakeHooks({ missing: () => ["gone_file"] });
    const sync = new ChartSync(s.api, h.hooks);
    await sync.open(SRC, A);
    h.setList([entry("ema"), entry("rsi")]); // what the layer saves: its other instances
    sync.changed();
    await sync.settled();
    expect(s.calls.at(-1)).toBe(`put ${A} rev=3 ids=ema,rsi`);
    expect(h.shown.at(-1)).toMatchObject({ why: "dropped" });
    expect(h.shown.at(-1)?.chart.indicators.map((i) => i.id)).toEqual(["ema", "rsi"]);
    expect(h.events).toContainEqual({ type: "dropped", instrument: A, ids: ["gone_file"] });
  });

  it("a save made on a chart the user has since left is not read back onto the new one", async () => {
    const s = fakeServer({ [key(SRC, A)]: { revision: 1, indicators: [inst("ema")] }, [key(SRC, B)]: { revision: 5, indicators: [inst("rsi")] } });
    const h = fakeHooks({ missing: () => ["gone_file"] });
    const sync = new ChartSync(s.api, h.hooks);
    await sync.open(SRC, A);
    h.setList([entry("ema"), entry("macd")]);
    s.hold();
    sync.changed();
    const opening = sync.open(SRC, B);
    s.release();
    await opening;
    await sync.settled();
    expect(s.calls).toContain(`put ${A} rev=1 ids=ema,macd`);
    expect(h.shown.at(-1)?.chart.instrument).toBe(B);
    expect(h.events.some((e) => e.type === "dropped")).toBe(false);
  });

  it("without such an instance a save is not read back", async () => {
    const { s, h, sync } = await opened(3);
    const n = h.shown.length;
    h.setList([entry("ema"), entry("rsi")]);
    sync.changed();
    await sync.settled();
    expect(h.shown.length).toBe(n);
    expect(s.api.get).toHaveBeenCalledTimes(1);
  });

  it("settled() waits for every save", async () => {
    const { s, h, sync } = await opened(3);
    s.hold();
    h.setList([entry("rsi")]);
    sync.changed();
    let done = false;
    void sync.settled().then(() => (done = true));
    await flush();
    expect(done).toBe(false);
    s.release();
    await sync.settled();
    expect(done).toBe(true);
  });
});

describe("messages from other browsers", () => {
  async function opened() {
    const s = fakeServer({ [key(SRC, A)]: { revision: 3, indicators: [inst("ema")] } });
    const h = fakeHooks();
    const sync = new ChartSync(s.api, h.hooks);
    await sync.open(SRC, A);
    return { s, h, sync };
  }
  const msg = (revision: number, ids: string[], instrument = A): ChartIndicators => ({ source: SRC, instrument, revision, indicators: ids.map((i) => inst(i)) });

  it("a newer list for this chart is shown", async () => {
    const { h, sync } = await opened();
    sync.receive(msg(4, ["macd"]));
    expect(h.shown.at(-1)).toMatchObject({ why: "remote" });
    expect(h.events).toContainEqual({ type: "remote", instrument: A });
  });

  it("our own write coming back is not shown again", async () => {
    const { h, sync } = await opened();
    h.setList([entry("ema"), entry("rsi")]);
    sync.changed();
    await sync.settled();
    const shownBefore = h.shown.length;
    sync.receive(msg(4, ["ema", "rsi"]));
    expect(h.shown.length).toBe(shownBefore);
  });

  it("a list for another chart is not shown", async () => {
    const { h, sync } = await opened();
    const n = h.shown.length;
    sync.receive(msg(9, ["macd"], B));
    expect(h.shown.length).toBe(n);
  });

  it("a newer list drops an edit that was not sent yet", async () => {
    const { s, h, sync } = await opened();
    s.hold();
    h.setList([entry("ema"), entry("rsi")]);
    sync.changed(); // goes out, held
    h.setList([entry("ema"), entry("rsi"), entry("macd")]);
    sync.changed(); // waits behind it
    s.set(SRC, A, 4, [inst("zz")]); // another browser wrote first
    sync.receive(msg(4, ["zz"]));
    s.release();
    await sync.settled();
    expect(s.calls.filter((c) => c.startsWith("put"))).toHaveLength(1);
    expect(h.shown.at(-1)?.chart.indicators.map((i) => i.id)).toEqual(["zz"]);
  });

  it("a newer list that arrives while a save is under way drops the edit queued behind it", async () => {
    const { s, h, sync } = await opened();
    s.hold();
    h.setList([entry("ema"), entry("rsi")]);
    sync.changed(); // goes out, held
    h.setList([entry("zz")]);
    sync.changed(); // waits behind it
    sync.receive(msg(5, ["macd"])); // another browser wrote after our save
    s.release();
    await sync.settled();
    expect(s.calls.filter((c) => c.startsWith("put"))).toHaveLength(1);
    expect(h.shown.at(-1)?.chart.indicators.map((i) => i.id)).toEqual(["macd"]);
  });

  it("a newer list for a chart that is no longer open drops its queued edit too", async () => {
    const { s, h, sync } = await opened();
    s.set(SRC, B, 1, [inst("rsi")]);
    s.hold();
    h.setList([entry("ema"), entry("rsi")]);
    sync.changed(); // goes out, held
    h.setList([entry("zz")]);
    sync.changed(); // waits behind it
    const away = sync.open(SRC, B); // the user moves on
    sync.receive(msg(5, ["macd"])); // then another browser wrote to the chart that was left
    s.release();
    await away;
    await sync.settled();
    expect(s.calls.filter((c) => c.startsWith("put"))).toHaveLength(1);
  });

  it("our own write coming back before its answer is not shown either", async () => {
    const { s, h, sync } = await opened();
    s.hold();
    h.setList([entry("ema"), entry("rsi")]);
    sync.changed();
    const n = h.shown.length;
    sync.receive(msg(4, ["ema", "rsi"])); // the broadcast beats the answer
    s.release();
    await sync.settled();
    expect(h.shown.length).toBe(n);
    expect(h.events.some((e) => e.type === "remote")).toBe(false);
  });

  it("after a reconnect the list is read again and a newer one is shown", async () => {
    const { s, h, sync } = await opened();
    s.set(SRC, A, 6, [inst("rsi")]);
    await sync.resync();
    expect(h.shown.at(-1)).toMatchObject({ why: "remote" });
    const n = h.shown.length;
    await sync.resync();
    expect(h.shown.length).toBe(n);
  });
});

describe("loading a layout", () => {
  it("puts the layout's list on the chart and lists what could not be used", async () => {
    const s = fakeServer({ [key(SRC, A)]: { revision: 3, indicators: [inst("ema")] } });
    const h = fakeHooks();
    const sync = new ChartSync(s.api, h.hooks);
    await sync.open(SRC, A);
    await sync.applyLayout("L1", "Gold H1", SRC, A);
    expect(s.calls.at(-1)).toBe(`apply ${A}`);
    expect(h.shown.at(-1)).toMatchObject({ why: "apply" });
    expect(h.shown.at(-1)?.chart.indicators.map((i) => i.id)).toEqual(["rsi"]);
    expect(h.events).toContainEqual({ type: "applied", instrument: A, layout: "Gold H1", skipped: [{ id: "gone", kind: "custom", reason: "no indicator with this id" }] });
  });

  it("makes that chart the current one even when it was not open", async () => {
    const s = fakeServer({ [key(SRC, A)]: { revision: 3, indicators: [inst("ema")] } });
    const h = fakeHooks();
    const sync = new ChartSync(s.api, h.hooks);
    await sync.open(SRC, A);
    await sync.applyLayout("L1", "Gold H1", SRC, B);
    h.setList([entry("rsi"), entry("macd")]);
    sync.changed();
    await sync.settled();
    expect(s.calls.at(-1)).toBe(`put ${B} rev=1 ids=rsi,macd`);
  });

  it("drops an edit that was waiting to be sent", async () => {
    const s = fakeServer({ [key(SRC, A)]: { revision: 3, indicators: [inst("ema")] } });
    const h = fakeHooks();
    const sync = new ChartSync(s.api, h.hooks);
    await sync.open(SRC, A);
    s.hold();
    h.setList([entry("ema"), entry("rsi")]);
    sync.changed();
    h.setList([entry("zz")]);
    sync.changed();
    const applying = sync.applyLayout("L1", "Gold H1", SRC, A);
    s.release();
    await applying;
    await sync.settled();
    expect(s.calls.filter((c) => c.startsWith("put"))).toHaveLength(1);
  });

  it("when the server cannot apply it, says so and reads the chart's own list", async () => {
    const s = fakeServer({ [key(SRC, A)]: { revision: 3, indicators: [inst("ema")] } });
    const h = fakeHooks();
    const sync = new ChartSync(s.api, h.hooks);
    await sync.open(SRC, A);
    s.api.fail = (what) => (what === "apply" ? apiErrorFrom(404, "Not Found", { detail: "Not Found" }) : null);
    await sync.applyLayout("L1", "Gold H1", SRC, A);
    expect(h.events).toContainEqual({ type: "failed", instrument: A, message: "Not Found" });
    expect(s.calls.slice(-2)).toEqual([`apply ${A}`, `get ${A}`]);
    expect(h.shown.at(-1)?.chart.indicators.map((i) => i.id)).toEqual(["ema"]);
  });
});
