import { describe, expect, it } from "vitest";

import type { CustomEntry, IndicatorCatalog, IndicatorDef } from "../api";
import { pickerModel } from "./pickerModel";

const def = (id: string, title: string, over: Partial<IndicatorDef> = {}): IndicatorDef => ({
  id,
  name: title.toUpperCase(),
  title,
  overlay: true,
  params: [],
  outputs: [],
  builtin: true,
  ...over,
});

const own = (id: string, over: Partial<CustomEntry> = {}): CustomEntry => ({
  id,
  kind: "custom",
  file: `${id}.py`,
  status: "ok",
  reason: null,
  line: null,
  definition: def(id, id.replace("_", " "), { builtin: false, overlay: false }),
  ...over,
});

const EMA = def("ema", "Exponential Moving Average", { name: "EMA" });
const EDIT_ON = { allowed: true, setting: "on", reason: "turned on on the server" };
const EDIT_OFF = { allowed: false, setting: "off", reason: "turned off on the server (openchart settings set indicator-editing on|auto|off)" };

function catalog(over: Partial<IndicatorCatalog> = {}): IndicatorCatalog {
  const custom = over.custom ?? [own("sma_cross")];
  return {
    indicators: [EMA, ...custom.flatMap((c) => (c.status === "ok" && c.definition ? [c.definition] : []))],
    errors: [],
    folder: "/data/indicators",
    builtin: [EMA],
    custom,
    isolation: { level: "appcontainer", minimum_met: true, notice: null, notes: [] },
    worker: { state: "running" },
    editing: EDIT_ON,
    ...over,
  };
}

const rows = (m: ReturnType<typeof pickerModel>, kind: "builtin" | "custom") => m.sections.find((s) => s.kind === kind)?.rows ?? [];

describe("sections", () => {
  it("are Built-in and Your files, built-in first, each row with what the list shows", () => {
    const m = pickerModel(catalog(), "");
    expect(m.sections.map((s) => [s.kind, s.heading])).toEqual([
      ["builtin", "Built-in"],
      ["custom", "Your files"],
    ]);
    const [ema] = rows(m, "builtin");
    expect(ema).toMatchObject({ key: "builtin:ema", id: "ema", title: "Exponential Moving Average", name: "EMA", where: "on the chart", state: "ok", addable: true, def: EMA, canDelete: null });
    const [sma] = rows(m, "custom");
    expect(sma).toMatchObject({ key: "custom:sma_cross.py", id: "sma_cross", file: "sma_cross.py", where: "own pane", state: "ok", addable: true, why: null });
    expect(sma.def?.id).toBe("sma_cross");
  });

  it("keep the title order inside a section, files that cannot be added after those that can", () => {
    const m = pickerModel(
      catalog({
        custom: [
          own("zeta"),
          own("broken", { status: "error", reason: "SyntaxError", definition: null }),
          own("alpha"),
        ],
      }),
      "",
    );
    expect(rows(m, "custom").map((r) => r.id)).toEqual(["alpha", "zeta", "broken"]);
  });

  it("show Your files with a hint when there are none, and nothing else", () => {
    const m = pickerModel(catalog({ custom: [], indicators: [EMA] }), "");
    const s = m.sections.find((x) => x.kind === "custom")!;
    expect(s.rows).toEqual([]);
    expect(s.hint).toBe("No files of yours yet.");
  });
});

describe("search", () => {
  it("matches title, name, id and file, any case, every word", () => {
    const c = catalog({ custom: [own("sma_cross")] });
    expect(rows(pickerModel(c, "exponential"), "builtin")).toHaveLength(1);
    expect(rows(pickerModel(c, "EMA"), "builtin")).toHaveLength(1);
    expect(rows(pickerModel(c, "sma_cross.py"), "custom")).toHaveLength(1);
    expect(rows(pickerModel(c, "SMA CROSS"), "custom")).toHaveLength(1);
    expect(rows(pickerModel(c, "moving average"), "builtin")).toHaveLength(1);
    expect(rows(pickerModel(c, "moving banana"), "builtin")).toHaveLength(0);
  });

  it("ignores surrounding space and finds across both sections", () => {
    const m = pickerModel(catalog({ custom: [own("ema_fast")] }), "  ema ");
    expect(rows(m, "builtin")).toHaveLength(1);
    expect(rows(m, "custom").map((r) => r.id)).toEqual(["ema_fast"]);
  });

  it("hides a section with no match, and says so when nothing matches", () => {
    const m = pickerModel(catalog(), "sma");
    expect(m.sections.map((s) => s.kind)).toEqual(["custom"]);
    const none = pickerModel(catalog(), "zzz");
    expect(none.sections).toEqual([]);
    expect(none.empty).toBe("No indicator matches.");
    expect(pickerModel(catalog(), "").empty).toBeNull();
  });

  it("finds a file that did not load by its file name", () => {
    const c = catalog({ custom: [own("bad", { status: "error", reason: "SyntaxError", definition: null })] });
    expect(rows(pickerModel(c, "bad.py"), "custom")).toHaveLength(1);
  });
});

describe("the states of a file", () => {
  it("error: not addable, the reason and the line are shown", () => {
    const c = catalog({ custom: [own("bad", { status: "error", reason: "SyntaxError: invalid syntax", line: 12, definition: null })] });
    const [r] = rows(pickerModel(c, ""), "custom");
    expect(r).toMatchObject({ state: "error", addable: false, def: null, why: "SyntaxError: invalid syntax", line: 12, title: "bad", name: "bad" });
  });

  it("error without a reason still says something", () => {
    const c = catalog({ custom: [own("bad", { status: "error", reason: null, definition: null })] });
    expect(rows(pickerModel(c, ""), "custom")[0].why).toBe("The file did not load.");
  });

  it("quarantined: not addable, says it was switched off and how to retry", () => {
    const c = catalog({ custom: [own("flaky", { status: "quarantined", reason: "failed 3 times in a row", definition: null })] });
    const [r] = rows(pickerModel(c, ""), "custom");
    expect(r).toMatchObject({ state: "quarantined", addable: false });
    expect(r.why).toBe("Switched off after repeated failures (failed 3 times in a row). Edit the file to try again.");
  });

  it("not_run: not addable, says the worker is not available, with its error when it has one", () => {
    const c = catalog({ custom: [own("idle", { status: "not_run", reason: null, definition: null })], worker: { state: "failed", error: "could not start" } });
    const [r] = rows(pickerModel(c, ""), "custom");
    expect(r).toMatchObject({ state: "not_run", addable: false });
    expect(r.why).toBe("The indicator worker is not available: could not start");
    const quiet = catalog({ custom: [own("idle", { status: "not_run", reason: null, definition: null })], worker: null });
    expect(rows(pickerModel(quiet, ""), "custom")[0].why).toBe("The indicator worker is not available.");
  });

  it("a file with a built-in's id cannot be added yet", () => {
    const c = catalog({ custom: [own("ema", { definition: def("ema", "My EMA", { builtin: false }) })] });
    const [r] = rows(pickerModel(c, ""), "custom");
    expect(r).toMatchObject({ state: "shadowed", addable: false, def: null, title: "My EMA" });
    expect(r.why).toBe("Same id as a built-in indicator, so it cannot be added yet.");
  });

  it("status ok without a definition is treated as a file that did not load", () => {
    const c = catalog({ custom: [own("odd", { status: "ok", definition: null })] });
    expect(rows(pickerModel(c, ""), "custom")[0]).toMatchObject({ state: "error", addable: false });
  });
});

describe("who may delete", () => {
  it("every file of yours has the editing gate: allowed, or the server's reason", () => {
    const on = rows(pickerModel(catalog(), ""), "custom")[0];
    expect(on.canDelete).toEqual({ allowed: true, reason: null });
    const off = rows(pickerModel(catalog({ editing: EDIT_OFF }), ""), "custom")[0];
    expect(off.canDelete).toEqual({ allowed: false, reason: EDIT_OFF.reason });
  });

  it("a file that did not load can be deleted too", () => {
    const c = catalog({ custom: [own("bad", { status: "error", reason: "x", definition: null })] });
    expect(rows(pickerModel(c, ""), "custom")[0].canDelete).toEqual({ allowed: true, reason: null });
  });

  it("a server that does not say gives a reason, not a guess", () => {
    const c = catalog();
    delete c.editing;
    expect(rows(pickerModel(c, ""), "custom")[0].canDelete).toEqual({
      allowed: false,
      reason: "This server does not say whether editing is allowed.",
    });
  });
});

describe("notices", () => {
  it("the isolation notice and the worker's own-folder notes sit on Your files", () => {
    const c = catalog({
      isolation: { level: "restricted_token", minimum_met: true, notice: "Weaker isolation: a restricted token only.", notes: ["its own folder is writable", "something else"] },
    });
    const s = pickerModel(c, "").sections.find((x) => x.kind === "custom")!;
    expect(s.notices).toEqual(["Weaker isolation: a restricted token only.", "its own folder is writable"]);
    expect(pickerModel(catalog(), "").sections.find((x) => x.kind === "custom")!.notices).toEqual([]);
  });

  it("a notice is kept when a search leaves the section's rows", () => {
    const c = catalog({ isolation: { level: "x", notice: "Weaker isolation.", notes: [] } });
    const s = pickerModel(c, "sma").sections.find((x) => x.kind === "custom")!;
    expect(s.notices).toEqual(["Weaker isolation."]);
  });
});

describe("errors the server lists besides the files", () => {
  it("a built-in's load error is a notice on Built-in", () => {
    const c = catalog({ errors: [{ file: "rsi.py", error: "boom", kind: "builtin" }] });
    expect(pickerModel(c, "").sections[0].notices).toEqual(["rsi.py: boom"]);
  });

  it("an error for a file already listed in custom is not shown twice", () => {
    const bad = own("bad", { status: "error", reason: "SyntaxError", definition: null });
    const c = catalog({ custom: [bad], errors: [{ file: "bad.py", error: "SyntaxError", kind: "custom" }] });
    const m = pickerModel(c, "");
    expect(rows(m, "custom")).toHaveLength(1);
    expect(m.sections[0].notices).toEqual([]);
  });
});

describe("a server from before the sections (flat list and errors only)", () => {
  const old: IndicatorCatalog = {
    indicators: [EMA, def("mine", "Mine", { builtin: false })],
    errors: [{ file: "bad.py", error: "SyntaxError: x" }],
    folder: "/data/indicators",
  };

  it("builds both sections from the flat list and the errors", () => {
    const m = pickerModel(old, "");
    expect(rows(m, "builtin").map((r) => r.id)).toEqual(["ema"]);
    expect(rows(m, "custom").map((r) => [r.id, r.state, r.addable])).toEqual([
      ["mine", "ok", true],
      ["bad", "error", false],
    ]);
    expect(rows(m, "custom")[1].why).toBe("SyntaxError: x");
  });

  it("cannot say who may delete", () => {
    expect(rows(pickerModel(old, ""), "custom")[0].canDelete?.allowed).toBe(false);
  });
});
