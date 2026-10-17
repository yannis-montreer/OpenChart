import { describe, expect, it } from "vitest";

import type { CustomEntry, IndicatorCatalog, IndicatorDef } from "../api";
import { pickerModel } from "./pickerModel";

const def = (id: string, builtin: boolean): IndicatorDef => ({ id, name: id, title: id, overlay: true, params: [], outputs: [], builtin });
const own = (id: string, over: Partial<CustomEntry> = {}): CustomEntry => ({
  id,
  kind: "custom",
  file: `${id}.py`,
  status: "ok",
  reason: null,
  line: null,
  definition: def(id, false),
  ...over,
});
const ON = { allowed: true, setting: "on", reason: "turned on" };
const OFF = { allowed: false, setting: "off", reason: "turned off on the server (openchart settings set indicator-editing on|auto|off)" };

function catalog(custom: CustomEntry[], editing: IndicatorCatalog["editing"] = ON): IndicatorCatalog {
  return { indicators: [def("ema", true)], errors: [], folder: "/data/indicators", builtin: [def("ema", true)], custom, editing };
}
const section = (c: IndicatorCatalog, kind: "builtin" | "custom", q = "") => pickerModel(c, q).sections.find((s) => s.kind === kind);

describe("the actions on a file of yours", () => {
  it("are Edit, Rename, Duplicate and Export, allowed together while editing is on", () => {
    const row = section(catalog([own("mine")]), "custom")!.rows[0];
    for (const name of ["edit", "rename", "duplicate", "export"] as const) {
      expect(row.actions?.[name], name).toEqual({ allowed: true, reason: null });
    }
  });

  it("are all off with the server's reason when editing is off", () => {
    const row = section(catalog([own("mine")], OFF), "custom")!.rows[0];
    for (const name of ["edit", "rename", "duplicate", "export"] as const) {
      expect(row.actions?.[name], name).toEqual({ allowed: false, reason: OFF.reason });
    }
  });

  it("are off with a stated reason when the server does not say (an older server)", () => {
    const row = section({ ...catalog([own("mine")]), editing: undefined }, "custom")!.rows[0];
    expect(row.actions?.edit).toEqual({ allowed: false, reason: "This server does not say whether editing is allowed." });
  });

  it("are there for a file that did not load, is switched off or has no worker: that is when you need Edit", () => {
    const rows = section(
      catalog([
        own("broken", { status: "error", reason: "SyntaxError", definition: null, line: 3 }),
        own("loud", { status: "quarantined", reason: "timeout", definition: null }),
        own("idle", { status: "not_run", definition: null }),
      ]),
      "custom",
    )!.rows;
    expect(rows).toHaveLength(3);
    for (const r of rows) expect(r.actions?.edit).toEqual({ allowed: true, reason: null });
  });

  it("are there for a file whose id is a built-in's (the two are separate namespaces)", () => {
    const row = section(catalog([own("ema", { definition: def("ema", false) })]), "custom")!.rows[0];
    expect(row.state).toBe("shadowed");
    expect(row.actions?.edit.allowed).toBe(true);
  });

  it("do not exist on a built-in indicator", () => {
    expect(section(catalog([own("mine")]), "builtin")!.rows[0].actions).toBeNull();
  });
});

describe("New and Import", () => {
  it("are allowed with editing, and carry the reason without it", () => {
    expect(section(catalog([own("mine")]), "custom")!.canCreate).toEqual({ allowed: true, reason: null });
    expect(section(catalog([own("mine")], OFF), "custom")!.canCreate).toEqual({ allowed: false, reason: OFF.reason });
  });

  it("are there when no file exists yet, and while a search matches nothing", () => {
    expect(section(catalog([]), "custom")!.canCreate?.allowed).toBe(true);
    // A search that finds nothing shows no section, so New and Import also sit on the model itself.
    const none = pickerModel(catalog([own("mine")]), "zzz");
    expect(none.sections).toEqual([]);
    expect(none.create).toEqual({ allowed: true, reason: null });
    expect(pickerModel(catalog([own("mine")], OFF), "zzz").create).toEqual({ allowed: false, reason: OFF.reason });
  });

  it("do not belong to the built-in section", () => {
    expect(section(catalog([]), "builtin")!.canCreate).toBeNull();
  });
});

describe("the editing note", () => {
  it("says once, for the section, why editing is off", () => {
    expect(section(catalog([own("mine")], OFF), "custom")!.editNote).toBe(`Editing is off: ${OFF.reason}`);
  });

  it("is absent while editing is on", () => {
    expect(section(catalog([own("mine")]), "custom")!.editNote).toBeNull();
  });

  it("says so for an older server that does not report it", () => {
    expect(section({ ...catalog([own("mine")]), editing: undefined }, "custom")!.editNote).toBe("Editing is off: This server does not say whether editing is allowed.");
  });
});
