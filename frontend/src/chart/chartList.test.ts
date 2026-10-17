import { describe, expect, it } from "vitest";

import type { ChartIndicator, ChartIndicators, IndicatorEntry } from "../api";
import {
  askToImport,
  fromServer,
  ghostOf,
  legendState,
  legacyDone,
  legacyEntries,
  newUid,
  resetItems,
  syncToast,
  toServer,
  wsAction,
} from "./chartList";

const ci = (over: Partial<ChartIndicator> = {}): ChartIndicator => ({
  uid: "u1",
  kind: "builtin",
  id: "ema",
  params: { length: 9 },
  visible: true,
  price_labels: true,
  legend_values: true,
  status: "ok",
  reset: [],
  ...over,
});

const chart = (over: Partial<ChartIndicators> = {}): ChartIndicators => ({
  source: "oanda",
  instrument: "EUR_USD",
  revision: 3,
  indicators: [ci()],
  ...over,
});

const entry = (over: Partial<IndicatorEntry> = {}): IndicatorEntry => ({
  id: "ema",
  params: { length: 9 },
  visible: true,
  price_labels: true,
  legend_values: true,
  ...over,
});

describe("what the server's instance becomes in the browser", () => {
  it("keeps uid and kind, drops status and reset from the entry", () => {
    const got = fromServer(ci({ uid: "a-1", kind: "custom", id: "sma_cross", status: "ok" }));
    expect(got.entry).toEqual({ uid: "a-1", kind: "custom", id: "sma_cross", params: { length: 9 }, visible: true, price_labels: true, legend_values: true });
    expect(got.state).toBeNull();
    expect(got.reset).toEqual([]);
  });

  it.each([
    ["missing", "missing"],
    ["error", "error"],
    ["quarantined", "quarantined"],
    ["not_run", "not_run"],
  ] as const)("status %s is a placeholder in state %s", (status, state) => {
    expect(fromServer(ci({ status })).state).toBe(state);
  });

  it("carries the inputs that were reset", () => {
    expect(fromServer(ci({ reset: ["length", "source"] })).reset).toEqual(["length", "source"]);
  });
});

describe("what goes to the server", () => {
  it("is the whole list in order, each with uid, kind and its settings", () => {
    const out = toServer([entry({ uid: "b", kind: "builtin" }), entry({ id: "mine", uid: "a", kind: "custom", visible: false })]);
    expect(out.map((e) => [e.uid, e.kind, e.id, e.visible])).toEqual([
      ["b", "builtin", "ema", true],
      ["a", "custom", "mine", false],
    ]);
    expect(out[0]).toEqual({ uid: "b", kind: "builtin", id: "ema", params: { length: 9 }, visible: true, price_labels: true, legend_values: true });
  });

  it("leaves out uid and kind an entry does not have (the server fills them)", () => {
    const [out] = toServer([entry()]);
    expect("uid" in out).toBe(false);
    expect("kind" in out).toBe(false);
  });

  it("does not share the params object with the entry", () => {
    const e = entry();
    const [out] = toServer([e]);
    out.params.length = 99;
    expect(e.params.length).toBe(9);
  });
});

describe("new uids", () => {
  it("are accepted by the server's rule and differ", () => {
    const a = newUid();
    const b = newUid();
    expect(a).not.toBe(b);
    for (const u of [a, b]) expect(u).toMatch(/^[A-Za-z0-9_-]{1,40}$/);
  });
});

describe("the placeholder states", () => {
  it("each says what is wrong and what to do", () => {
    expect(legendState("missing")).toEqual({ label: "Not found", tip: expect.stringContaining("no indicator") });
    // CI-3 steps 4 to 6: such an instance is left out of the next save, so the tip must not say it stays.
    expect(legendState("missing").tip).not.toMatch(/stays in the list/);
    expect(legendState("error").label).toBe("File error");
    expect(legendState("quarantined").label).toBe("Switched off");
    expect(legendState("quarantined").tip).toContain("Edit the file");
    expect(legendState("not_run").label).toBe("Worker off");
    expect(legendState("shadowed").label).toBe("Built-in has this id");
  });

  it("an instance whose kind disagrees with the definition the catalog holds is a shadowed one", () => {
    expect(ghostOf({ kind: "custom", id: "ema" }, { builtin: true })).toBe("shadowed");
    expect(ghostOf({ kind: "builtin", id: "ema" }, { builtin: true })).toBeNull();
    expect(ghostOf({ kind: "custom", id: "x" }, { builtin: false })).toBeNull();
    expect(ghostOf({ kind: undefined, id: "x" }, { builtin: false })).toBeNull();
    expect(ghostOf({ kind: undefined, id: "ema" }, { builtin: true })).toBeNull();
  });

  it("an instance without a definition in the catalog is missing", () => {
    expect(ghostOf({ kind: "custom", id: "x" }, undefined)).toBe("missing");
  });
});

describe("the old browser list", () => {
  it("keeps entries that look like an instance and drops the rest", () => {
    const got = legacyEntries([entry(), { id: 5 }, null, "x", { id: "rsi", params: {}, visible: true }, { params: {} }]);
    expect(got.map((e) => (e as { id: string }).id)).toEqual(["ema", "rsi"]);
  });

  it("is nothing for what is not a list", () => {
    expect(legacyEntries(undefined)).toEqual([]);
    expect(legacyEntries({})).toEqual([]);
    expect(legacyEntries("ema")).toEqual([]);
  });

  it("is asked to import only into a chart never set and only when it holds something", () => {
    expect(askToImport([entry()], chart({ revision: 0, indicators: [] }))).toBe(true);
    expect(askToImport([], chart({ revision: 0, indicators: [] }))).toBe(false);
    expect(askToImport([entry()], chart({ revision: 1, indicators: [] }))).toBe(false);
    expect(askToImport([entry()], chart({ revision: 3 }))).toBe(false);
  });

  it("is done when every enabled chart has a list, and not before", () => {
    expect(legacyDone([3, 1, 7])).toBe(true);
    expect(legacyDone([3, 0, 7])).toBe(false);
    expect(legacyDone([])).toBe(false);
  });
});

describe("messages from the server", () => {
  const here = { source: "oanda", instrument: "EUR_USD" };
  const msg = (over: Partial<ChartIndicators> = {}) => chart({ revision: 5, ...over });

  it("another chart: remember its revision only", () => {
    expect(wsAction(msg({ instrument: "XAU_USD" }), here, 3)).toBe("note");
  });

  it("the same instrument on another source is another chart", () => {
    expect(wsAction(msg({ source: "other" }), here, 3)).toBe("note");
  });

  it("this chart, newer: take it", () => {
    expect(wsAction(msg(), here, 3)).toBe("take");
  });

  it("this chart, the revision we already have (our own write coming back): ignore", () => {
    expect(wsAction(msg({ revision: 3 }), here, 3)).toBe("ignore");
    expect(wsAction(msg({ revision: 2 }), here, 3)).toBe("ignore");
  });

  it("nothing is taken before the chart has been opened", () => {
    expect(wsAction(msg(), null, 0)).toBe("note");
  });
});

describe("notes in words", () => {
  const titleOf = (id: string) => (id === "ema" ? "EMA" : id);

  it("lists the inputs that were reset, per indicator", () => {
    const items = resetItems(
      chart({ indicators: [ci({ id: "ema", reset: ["length"] }), ci({ uid: "u2", id: "mine", reset: ["a", "b"] }), ci({ uid: "u3", id: "rsi" })] }),
      titleOf,
    );
    expect(items).toEqual(["EMA: length", "mine: a, b"]);
  });

  it("no inputs reset, no items", () => {
    expect(resetItems(chart(), titleOf)).toEqual([]);
  });

  it("imported: says where the list came from and why it is per chart now", () => {
    const t = syncToast({ type: "imported", instrument: "EUR_USD", count: 2, skipped: [] });
    expect(t.title).toBe("Indicators copied to EUR_USD");
    expect(t.body).toBe("The 2 indicators this browser had are now EUR_USD's own list. They used to be shared by every chart; each chart keeps its own list on the server now.");
  });

  it("imported, one indicator, some left out", () => {
    const t = syncToast({ type: "imported", instrument: "XAU_USD", count: 1, skipped: [{ id: "gone", kind: "custom", reason: "no indicator with this id" }, { id: null, kind: null, reason: "not an indicator" }] });
    expect(t.body).toContain("The 1 indicator this browser had is now XAU_USD's own list.");
    expect(t.body).toContain("Left out: gone (no indicator with this id), not an indicator.");
  });

  it("conflict: says the list changed elsewhere and was reloaded", () => {
    expect(syncToast({ type: "conflict", instrument: "EUR_USD" })).toEqual({
      title: "Indicators changed elsewhere",
      body: "EUR_USD's list was changed from another browser. The newer list is shown; your last change was not saved.",
    });
  });

  it("remote: says it was updated", () => {
    expect(syncToast({ type: "remote", instrument: "EUR_USD" }).title).toBe("Indicators updated");
  });

  it("reset: names the inputs and says the next change keeps the defaults", () => {
    const t = syncToast({ type: "reset", instrument: "EUR_USD", items: ["EMA: length", "mine: a, b"] });
    expect(t.title).toBe("Settings reset");
    expect(t.body).toBe("The definition changed, so these went back to their defaults: EMA: length; mine: a, b. Your next change on this chart keeps them.");
  });

  it("applied: lists what the layout could not put on the chart", () => {
    const t = syncToast({ type: "applied", instrument: "EUR_USD", layout: "Gold H1", skipped: [{ id: "gone", kind: "custom", reason: "no indicator with this id" }] });
    expect(t.title).toBe("Layout loaded with changes");
    expect(t.body).toBe('Left out of "Gold H1" on EUR_USD: gone (no indicator with this id).');
  });

  it("dropped: names what the server no longer has", () => {
    expect(syncToast({ type: "dropped", instrument: "EUR_USD", ids: ["gone_file", "other"] })).toEqual({
      title: "Removed from the list",
      body: "gone_file, other no longer exist on the server, so they were taken off EUR_USD's list.",
    });
    expect(syncToast({ type: "dropped", instrument: "EUR_USD", ids: ["gone_file"] }).body).toBe("gone_file no longer exists on the server, so it was taken off EUR_USD's list.");
  });

  it("failed: carries the reason", () => {
    expect(syncToast({ type: "failed", instrument: "EUR_USD", message: "Server down" })).toEqual({
      title: "Indicators not saved",
      body: "Could not save EUR_USD's indicators: Server down",
    });
  });

  it("unloaded: says changes are not kept until the page is reloaded", () => {
    expect(syncToast({ type: "unloaded", instrument: "EUR_USD" }).body).toContain("could not be loaded");
  });
});
