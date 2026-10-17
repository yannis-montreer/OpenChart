import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { ApiError, customApi } from "../api";
import { MockServer, mockVersion } from "./mockServer";

let mock: MockServer;

function install(opts?: ConstructorParameters<typeof MockServer>[0]) {
  mock = new MockServer(opts);
  vi.stubGlobal("window", { location: { href: "http://test/" }, dispatchEvent: () => true });
  vi.stubGlobal("fetch", mock.fetch);
}

beforeEach(() => install());
afterEach(() => vi.unstubAllGlobals());

async function failure(p: Promise<unknown>): Promise<ApiError> {
  try {
    await p;
  } catch (err) {
    expect(err).toBeInstanceOf(ApiError);
    return err as ApiError;
  }
  throw new Error("expected the call to fail");
}

describe("New, Import and the templates", () => {
  it("lists the templates", async () => {
    const list = await customApi.templates();
    expect(list.map((t) => t.id)).toEqual(["blank", "sma_cross", "rsi_bands", "bollinger_fill", "engulfing"]);
  });

  it("New copies a template under the id, with the title on its TITLE line", async () => {
    const made = await customApi.create({ id: "mine", template: "blank", title: "My own" });
    expect(made).toMatchObject({ id: "mine", file: "mine.py", status: "ok", saved: true, unchanged: false, error: null });
    expect(mock.read("mine")).toBe('TITLE = "My own"\n');
    expect(made.version).toBe(mockVersion('TITLE = "My own"\n'));
    expect(mock.broadcasts).toEqual([{ id: "mine", op: "create" }]);
  });

  it("New never overwrites: 409 with the first free id", async () => {
    mock.place("mine", "x");
    mock.place("mine_2", "y");
    const err = await failure(customApi.create({ id: "mine", template: "blank" }));
    expect(err.status).toBe(409);
    expect(err.detail).toMatchObject({ suggested: "mine_3" });
    expect(mock.read("mine")).toBe("x");
  });

  it("New refuses an id that is not one and a template that is not there", async () => {
    expect((await failure(customApi.create({ id: "Mine", template: "blank" }))).status).toBe(400);
    expect((await failure(customApi.create({ id: "mine", template: "nope" }))).status).toBe(400);
    expect(mock.files.size).toBe(0);
  });

  it("Import stores the source as it is and makes the id of the file name", async () => {
    const made = await customApi.importSource({ source: "a = 1\r\nb = 2\r\n", filename: "My RSI.py" });
    expect(made.id).toBe("my_rsi");
    expect(mock.read("my_rsi")).toBe("a = 1\r\nb = 2\r\n");
  });

  it("Import never overwrites, and says what is free", async () => {
    mock.place("imported", "x");
    const err = await failure(customApi.importSource({ source: "y" }));
    expect(err.status).toBe(409);
    expect(err.detail).toMatchObject({ suggested: "imported_2" });
    expect(mock.read("imported")).toBe("x");
  });

  it("Import drops a leading byte-order mark and refuses NUL and lone surrogates", async () => {
    await customApi.importSource({ source: "﻿TITLE = \"a\"\n", id: "bom" });
    expect(mock.read("bom")).toBe('TITLE = "a"\n');
    expect((await failure(customApi.importSource({ source: "a\u0000b", id: "nul" }))).status).toBe(400);
    expect((await failure(customApi.importSource({ source: "a\ud800", id: "sur" }))).status).toBe(400);
    expect(mock.files.has("nul") || mock.files.has("sur")).toBe(false);
  });

  it("Import refuses a source over 256 KB with 413 and exactly 256 KB passes", async () => {
    expect((await failure(customApi.importSource({ source: "a".repeat(256 * 1024 + 1), id: "big" }))).status).toBe(413);
    await customApi.importSource({ source: "a".repeat(256 * 1024), id: "edge" });
    expect(mock.files.has("big")).toBe(false);
    expect(mock.files.has("edge")).toBe(true);
  });
});

describe("Save and versions", () => {
  beforeEach(() => mock.place("mine", 'TITLE = "Mine"\nv = 1\n'));

  it("Save writes and keeps the old text as a version", async () => {
    const saved = await customApi.save("mine", { source: 'TITLE = "Mine"\nv = 2\n' });
    expect(saved).toMatchObject({ saved: true, unchanged: false });
    expect(mock.read("mine")).toBe('TITLE = "Mine"\nv = 2\n');
    const list = await customApi.versions("mine");
    expect(list).toHaveLength(1);
    expect((await customApi.version("mine", list[0].version)).source).toBe('TITLE = "Mine"\nv = 1\n');
  });

  it("Save of the stored text is 'unchanged': no version, no broadcast", async () => {
    const saved = await customApi.save("mine", { source: 'TITLE = "Mine"\nv = 1\n' });
    expect(saved.unchanged).toBe(true);
    expect(await customApi.versions("mine")).toEqual([]);
    expect(mock.broadcasts).toEqual([]);
  });

  it("Save never creates", async () => {
    expect((await failure(customApi.save("ghost", { source: "x" }))).status).toBe(404);
    expect(mock.files.has("ghost")).toBe(false);
  });

  it("a Save with a stale base_version is a 409 that names the stored version and changes nothing", async () => {
    const opened = await customApi.source("mine");
    mock.place("mine", "changed in a text editor");
    const err = await failure(customApi.save("mine", { source: "mine", base_version: opened.version }));
    expect(err.status).toBe(409);
    expect(err.detail).toMatchObject({ version: mockVersion("changed in a text editor") });
    expect(mock.read("mine")).toBe("changed in a text editor");
  });

  it("a Save without base_version always applies", async () => {
    mock.place("mine", "changed elsewhere");
    await customApi.save("mine", { source: "keep mine" });
    expect(mock.read("mine")).toBe("keep mine");
  });

  it("keeps the last 10 versions, newest first, and two saves in one second get -2", async () => {
    install({ clockStep: 0 });
    mock.place("mine", "v0");
    for (let i = 1; i <= 12; i++) await customApi.save("mine", { source: `v${i}` });
    const list = await customApi.versions("mine");
    expect(list).toHaveLength(10);
    expect(list.every((v) => /^20261017T080000Z(-\d+)?$/.test(v.version))).toBe(true);
    expect(list.filter((v) => v.version.includes("-"))).not.toHaveLength(0);
    expect(new Set(list.map((v) => v.version)).size).toBe(10);
    expect((await customApi.version("mine", list[0].version)).source).toBe("v11");
    expect((await customApi.version("mine", list[9].version)).source).toBe("v2");
  });

  it("Restore is a save: the current text goes into the history, so it can be undone", async () => {
    await customApi.save("mine", { source: "v2" });
    const [old] = await customApi.versions("mine");
    await customApi.restore("mine", old.version);
    expect(mock.read("mine")).toBe('TITLE = "Mine"\nv = 1\n');
    const after = await customApi.versions("mine");
    expect(after).toHaveLength(2);
    expect((await customApi.version("mine", after[0].version)).source).toBe("v2");
  });

  it("Restore of a version that is not there is a 404", async () => {
    expect((await failure(customApi.restore("mine", "20200101T000000Z"))).status).toBe(404);
  });

  it("Restore honours base_version", async () => {
    await customApi.save("mine", { source: "v2" });
    const [old] = await customApi.versions("mine");
    mock.place("mine", "elsewhere");
    expect((await failure(customApi.restore("mine", old.version, { base_version: mockVersion("v2") }))).status).toBe(409);
    expect(mock.read("mine")).toBe("elsewhere");
  });
});

describe("Rename, Duplicate, Export", () => {
  it("Rename rewrites the one TITLE line and keeps the id", async () => {
    mock.place("mine", 'TITLE = "Mine"\nx = 1\n');
    const saved = await customApi.rename("mine", "Better name");
    expect(saved.id).toBe("mine");
    expect(mock.read("mine")).toBe('TITLE = "Better name"\nx = 1\n');
    expect(mock.broadcasts).toEqual([{ id: "mine", op: "rename" }]);
  });

  it("Rename without exactly one TITLE line is a 409 'edit TITLE in the code'", async () => {
    mock.place("none", "x = 1\n");
    mock.place("two", 'TITLE = "a"\nTITLE = "b"\n');
    for (const id of ["none", "two"]) {
      const err = await failure(customApi.rename(id, "New"));
      expect([err.status, err.message]).toEqual([409, "edit TITLE in the code"]);
    }
    expect(mock.read("none")).toBe("x = 1\n");
  });

  it("Rename refuses a title of a line break or over 60 characters", async () => {
    mock.place("mine", 'TITLE = "Mine"\n');
    expect((await failure(customApi.rename("mine", "a\nb"))).status).toBe(400);
    expect((await failure(customApi.rename("mine", "a".repeat(61)))).status).toBe(400);
    expect(mock.read("mine")).toBe('TITLE = "Mine"\n');
  });

  it("Duplicate copies the file, not its history, under the first free id with '(copy)'", async () => {
    mock.place("mine", 'TITLE = "Mine"\n');
    await customApi.save("mine", { source: 'TITLE = "Mine"\nv = 2\n' });
    const made = await customApi.duplicate("mine");
    expect(made.id).toBe("mine_2");
    expect(mock.read("mine_2")).toBe('TITLE = "Mine (copy)"\nv = 2\n');
    expect(await customApi.versions("mine_2")).toEqual([]);
  });

  it("Duplicate to an id that exists is a 409 with a suggestion", async () => {
    mock.place("mine", 'TITLE = "Mine"\n');
    mock.place("other", "x");
    const err = await failure(customApi.duplicate("mine", { id: "other" }));
    expect(err.status).toBe(409);
    expect(err.detail).toMatchObject({ suggested: "other_2" });
  });

  it("Export is the file itself, from a same-origin path", async () => {
    mock.place("mine", "x = 1\n");
    expect(customApi.exportPath("mine")).toBe("api/custom-indicators/mine/source?download=true");
    const res = await mock.fetch(`http://test/${customApi.exportPath("mine")}`);
    expect(res.headers.get("Content-Disposition")).toBe('attachment; filename="mine.py"');
    expect(await res.text()).toBe("x = 1\n");
  });
});

describe("Run (preview)", () => {
  const q = { instrument: "XAU_USD", timeframe: "15m" };

  it("answers the values and the definition the source declares, and stores nothing", async () => {
    mock.place("mine", "stored");
    const got = await customApi.preview({ ...q, source: 'TITLE = "Draft"\n', id: "mine" });
    expect(got).toMatchObject({ preview: true, id: "mine", definition: { title: "Draft" } });
    expect(got.t.length).toBeGreaterThan(0);
    expect(mock.read("mine")).toBe("stored");
    expect(mock.history.size).toBe(0);
    expect(mock.broadcasts).toEqual([]);
  });

  it("a source that does not load or raises is a 422 with type, message, line and traceback", async () => {
    const err = await failure(customApi.preview({ ...q, source: "a = 1\nRAISE\n", id: "mine" }));
    expect(err.status).toBe(422);
    expect(err.detail).toMatchObject({ type: "ZeroDivisionError", line: 2 });
    expect((err.detail as { traceback: string }).traceback).toContain("mine.py");
  });

  it("is a 503 without the worker, and a 413 over 256 KB", async () => {
    install({ worker: false });
    expect((await failure(customApi.preview({ ...q, source: "x" }))).status).toBe(503);
    install();
    expect((await failure(customApi.preview({ ...q, source: "a".repeat(256 * 1024 + 1) }))).status).toBe(413);
  });
});

describe("editing off", () => {
  it("every call answers 403 with the reason, and nothing changes", async () => {
    install({ allowed: false, reason: "this server is open to your network" });
    mock.place("mine", 'TITLE = "Mine"\n');
    const calls: Promise<unknown>[] = [
      customApi.templates(),
      customApi.create({ id: "x", template: "blank" }),
      customApi.importSource({ source: "x", id: "y" }),
      customApi.source("mine"),
      customApi.save("mine", { source: "x" }),
      customApi.rename("mine", "n"),
      customApi.duplicate("mine"),
      customApi.versions("mine"),
      customApi.version("mine", "v"),
      customApi.restore("mine", "v"),
      customApi.preview({ source: "x", instrument: "a", timeframe: "b" }),
    ];
    for (const c of calls) {
      const err = await failure(c);
      expect(err.status).toBe(403);
      expect(err.message).toBe("Editing custom indicators is off: this server is open to your network.");
    }
    expect(mock.files.size).toBe(1);
    expect(mock.read("mine")).toBe('TITLE = "Mine"\n');
  });
});

describe("the catalog of the mock", () => {
  it("lists every file as the catalog's `custom` does: ok with its definition, or an error with its line", () => {
    mock.place("good", 'TITLE = "Good one"\n');
    mock.place("bad", "a = 1\nSYNTAX\n");
    const [good, bad] = mock.catalogEntries();
    expect(good).toMatchObject({ id: "good", kind: "custom", file: "good.py", status: "ok", reason: null, line: null, definition: { id: "good", title: "Good one" } });
    expect(bad).toMatchObject({ id: "bad", status: "error", reason: "SyntaxError: invalid syntax", line: 2, definition: null });
  });
});

describe("paths", () => {
  it("encode an id as one segment, so a name cannot reach another route", async () => {
    for (const id of ["../mine", "mine/source", "a?b", "a#b", "%2e%2e"]) {
      mock.calls.length = 0;
      const err = await failure(customApi.source(id));
      expect(err.status).toBe(404);
      const path = mock.calls[0].path;
      expect(path).not.toContain("/../");
      expect(path.split("?")[0].split("/")).toHaveLength(5); // "", api, custom-indicators, <id>, source
    }
  });

  it("encode a version as one segment when it is restored too", async () => {
    mock.place("mine", "x");
    mock.calls.length = 0;
    await failure(customApi.restore("mine", "../../x"));
    expect(mock.calls[0].path).toBe("/api/custom-indicators/mine/versions/..%2F..%2Fx/restore");
  });

  it("encode a version as one segment", async () => {
    mock.place("mine", "x");
    mock.calls.length = 0;
    await failure(customApi.version("mine", "../../x"));
    expect(mock.calls[0].path).toBe("/api/custom-indicators/mine/versions/..%2F..%2Fx");
  });
});
