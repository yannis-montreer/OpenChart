import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { customApi, type PreviewValues } from "../api";
import { readDraft, syncDraft, type DraftStore } from "./draft";
import { MockServer, mockVersion } from "./mockServer";
import { EditorSession, type EditorApi, type EditorState } from "./session";
import { MAX_SOURCE_BYTES } from "./rules";

class MemoryStore implements DraftStore {
  data = new Map<string, string>();
  getItem(k: string): string | null {
    return this.data.get(k) ?? null;
  }
  setItem(k: string, v: string): void {
    this.data.set(k, v);
  }
  removeItem(k: string): void {
    this.data.delete(k);
  }
}

const SRC = 'TITLE = "Mine"\nv = 1\n';
const QUERY = { instrument: "XAU_USD", timeframe: "15m", price: "mid" as const };

let mock: MockServer;
let store: MemoryStore;
let previews: (PreviewValues | null)[];
let previewSources: (string | undefined)[];
let states: EditorState[];

function make(opts: { api?: EditorApi; allowed?: boolean; id?: string } = {}) {
  const session = new EditorSession({
    id: opts.id ?? "mine",
    api: opts.api ?? customApi,
    store,
    editing: { allowed: opts.allowed ?? true, reason: opts.allowed === false ? "turned off" : null },
    onChange: (s) => states.push(s),
    onPreview: (p, source) => {
      previews.push(p);
      previewSources.push(source);
    },
  });
  return session;
}

beforeEach(() => {
  mock = new MockServer();
  mock.place("mine", SRC);
  store = new MemoryStore();
  previews = [];
  previewSources = [];
  states = [];
  vi.stubGlobal("window", { location: { href: "http://test/" }, dispatchEvent: () => true });
  vi.stubGlobal("fetch", mock.fetch);
});
afterEach(() => vi.unstubAllGlobals());

function deferred<T>() {
  let resolve!: (v: T) => void;
  let reject!: (e: unknown) => void;
  const promise = new Promise<T>((a, b) => {
    resolve = a;
    reject = b;
  });
  return { promise, resolve, reject };
}

describe("open", () => {
  it("reads the server's file, and the buffer is exactly that", async () => {
    const s = make();
    expect(s.state.phase).toBe("loading");
    await s.open();
    expect(s.state).toMatchObject({ phase: "ready", buffer: SRC, dirty: false, problem: null, draft: null });
    expect(s.state.stored).toEqual({ version: mockVersion(SRC), source: SRC });
  });

  it("a file that is not there is 'gone'", async () => {
    const s = make({ id: "ghost" });
    await s.open();
    expect(s.state.phase).toBe("gone");
  });

  it("editing off at the server (403) turns editing off with the server's reason, and shows nothing of the file", async () => {
    mock.allowed = false;
    mock.reason = "this server is open to your network";
    const s = make();
    await s.open();
    expect(s.state.phase).toBe("failed");
    expect(s.state.editing).toEqual({ allowed: false, reason: "Editing custom indicators is off: this server is open to your network." });
    expect(s.state.buffer).toBe("");
  });

  it("a network failure is 'failed' and open can be tried again", async () => {
    const api: EditorApi = { ...customApi, source: vi.fn().mockRejectedValueOnce(new Error("Failed to fetch")).mockImplementation(customApi.source) };
    const s = make({ api });
    await s.open();
    expect(s.state).toMatchObject({ phase: "failed", loadError: "Failed to fetch" });
    await s.open();
    expect(s.state.phase).toBe("ready");
  });

  it("offers a draft that started from this very version", async () => {
    syncDraft(store, "mine", "draft text", { version: mockVersion(SRC), source: SRC }, 7);
    const s = make();
    await s.open();
    expect(s.state.buffer).toBe(SRC); // the server's file is what opens
    expect(s.state.draft).toMatchObject({ source: "draft text" });
  });

  it("does not offer a draft of a version that is gone", async () => {
    syncDraft(store, "mine", "draft text", { version: "000000000000", source: "old" }, 7);
    const s = make();
    await s.open();
    expect(s.state.draft).toBeNull();
    expect(store.data.size).toBe(0);
  });

  it("restoring a draft puts it in the buffer as an unsaved edit; discarding drops it", async () => {
    syncDraft(store, "mine", "draft text", { version: mockVersion(SRC), source: SRC }, 7);
    const s = make();
    await s.open();
    s.restoreDraft();
    expect(s.state).toMatchObject({ buffer: "draft text", dirty: true, draft: null });
    expect(readDraft(store, "mine", s.state.stored!)).not.toBeNull(); // still kept until saved or discarded

    const t = make();
    await t.open();
    t.discardDraft();
    expect(t.state.draft).toBeNull();
    expect(store.data.size).toBe(0);
    expect(t.state.buffer).toBe(SRC);
  });
});

describe("edit", () => {
  it("marks the buffer dirty, keeps a draft, and is clean again when the text is back", async () => {
    const s = make();
    await s.open();
    s.edit("changed");
    expect(s.state).toMatchObject({ dirty: true, buffer: "changed" });
    expect(readDraft(store, "mine", s.state.stored!)?.source).toBe("changed");
    s.edit(SRC);
    expect(s.state.dirty).toBe(false);
    expect(store.data.size).toBe(0);
  });

  it("reports why a text cannot be saved or run", async () => {
    const s = make();
    await s.open();
    s.edit("a".repeat(MAX_SOURCE_BYTES + 1));
    expect(s.state.problem).toMatch(/256 KB/);
    s.edit("a\u0000");
    expect(s.state.problem).toMatch(/NUL/);
    s.edit("fine");
    expect(s.state.problem).toBeNull();
  });

  it("does nothing before the file is open", () => {
    const s = make();
    s.edit("x");
    expect(s.state.buffer).toBe("");
    expect(store.data.size).toBe(0);
  });
});

describe("save", () => {
  it("sends the buffer with the version it started from, and the saved text becomes the stored one", async () => {
    const s = make();
    await s.open();
    s.edit("v = 2\n");
    await s.save();
    const put = mock.calls.find((c) => c.method === "PUT")!;
    expect(put.body).toEqual({ source: "v = 2\n", base_version: mockVersion(SRC) });
    expect(s.state).toMatchObject({ dirty: false, saving: false, notice: { kind: "saved" } });
    expect(s.state.stored).toEqual({ version: mockVersion("v = 2\n"), source: "v = 2\n" });
    expect(store.data.size).toBe(0); // no draft of a saved text
  });

  it("an unchanged text says so", async () => {
    const s = make();
    await s.open();
    await s.save();
    expect(s.state.notice).toMatchObject({ kind: "unchanged" });
    expect(mock.history.size).toBe(0);
  });

  it("a file saved with an error is saved, and the error is shown with its line", async () => {
    const s = make();
    await s.open();
    s.edit("a = 1\nSYNTAX\n");
    await s.save();
    expect(mock.read("mine")).toBe("a = 1\nSYNTAX\n");
    expect(s.state.error).toMatchObject({ type: "SyntaxError", line: 2 });
    expect(s.state.notice).toMatchObject({ kind: "saved" });
    s.edit("a = 1\n");
    await s.save();
    expect(s.state.error).toBeNull();
  });

  it("makes no request for a text over the cap, with NUL, or when editing is off", async () => {
    const s = make();
    await s.open();
    mock.calls.length = 0;
    s.edit("a".repeat(MAX_SOURCE_BYTES + 1));
    await s.save();
    s.edit("a\u0000");
    await s.save();
    expect(mock.calls).toEqual([]);

    const off = make({ allowed: false });
    await off.open();
    expect(off.state.phase).toBe("ready"); // reading works; the gate is about writing
    off.edit("changed");
    await off.save();
    expect(mock.writes()).toEqual([]);
    expect(off.state.notice?.kind).toBe("off");
  });

  it("a file that changed elsewhere is a conflict: nothing is written and the buffer stays", async () => {
    const s = make();
    await s.open();
    s.edit("my edit");
    mock.place("mine", "changed in a text editor");
    await s.save();
    expect(mock.read("mine")).toBe("changed in a text editor");
    expect(s.state).toMatchObject({ buffer: "my edit", dirty: true, conflict: { version: mockVersion("changed in a text editor") }, notice: { kind: "conflict" } });
    expect(s.state.stored?.version).toBe(mockVersion(SRC));
  });

  it("'keep mine' saves over the file that changed, without a base version", async () => {
    const s = make();
    await s.open();
    s.edit("my edit");
    mock.place("mine", "changed in a text editor");
    await s.save();
    await s.save({ keepMine: true });
    expect(mock.read("mine")).toBe("my edit");
    expect(mock.calls.filter((c) => c.method === "PUT").at(-1)!.body).toEqual({ source: "my edit" });
    expect(s.state).toMatchObject({ conflict: null, dirty: false });
    // The text that was replaced is in the history, so it is not lost.
    const versions = await customApi.versions("mine");
    expect((await customApi.version("mine", versions[0].version)).source).toBe("changed in a text editor");
  });

  it("'reload' takes the server's file and drops the draft", async () => {
    const s = make();
    await s.open();
    s.edit("my edit");
    mock.place("mine", "changed in a text editor");
    await s.save();
    await s.reload();
    expect(s.state).toMatchObject({ buffer: "changed in a text editor", dirty: false, conflict: null });
    expect(store.data.size).toBe(0);
  });

  it("a deleted file (404) is 'gone', the buffer is kept, and Import puts it back under the same id", async () => {
    const s = make();
    await s.open();
    s.edit("my edit");
    mock.files.delete("mine");
    await s.save();
    expect(s.state).toMatchObject({ phase: "gone", buffer: "my edit", notice: { kind: "gone" } });
    await s.importBuffer();
    expect(mock.read("mine")).toBe("my edit");
    expect(s.state).toMatchObject({ phase: "ready", dirty: false });
    expect(mock.calls.filter((c) => c.method === "POST").at(-1)!.body).toEqual({ source: "my edit", id: "mine" });
  });

  it("Import of the buffer never overwrites: a file that came back is a conflict, not an overwrite", async () => {
    const s = make();
    await s.open();
    s.edit("my edit");
    mock.files.delete("mine");
    await s.save();
    mock.place("mine", "somebody else's");
    await s.importBuffer();
    expect(mock.read("mine")).toBe("somebody else's");
    expect(s.state.notice?.kind).toBe("failed");
    expect(s.state.phase).toBe("gone");
  });

  it("a 403 after the page thought editing was on turns editing off with the reason", async () => {
    const s = make();
    await s.open();
    s.edit("x");
    mock.allowed = false;
    await s.save();
    expect(s.state.editing).toMatchObject({ allowed: false });
    expect(s.state.notice).toMatchObject({ kind: "off" });
    expect(s.state.buffer).toBe("x");
  });

  it("a failed request keeps the buffer and can be tried again", async () => {
    const save = vi.fn().mockRejectedValueOnce(new Error("Failed to fetch")).mockImplementation(customApi.save);
    const s = make({ api: { ...customApi, save } });
    await s.open();
    s.edit("x");
    await s.save();
    expect(s.state).toMatchObject({ saving: false, dirty: true, notice: { kind: "failed", text: "Failed to fetch" } });
    await s.save();
    expect(s.state).toMatchObject({ dirty: false, notice: { kind: "saved" } });
  });

  it("only one save is in flight: a second call while it runs is ignored", async () => {
    const gate = deferred<void>();
    const save = vi.fn(async (...args: Parameters<typeof customApi.save>) => {
      await gate.promise;
      return customApi.save(...args);
    });
    const s = make({ api: { ...customApi, save } });
    await s.open();
    s.edit("x");
    const first = s.save();
    const second = s.save();
    gate.resolve();
    await Promise.all([first, second]);
    expect(save).toHaveBeenCalledTimes(1);
  });

  it("an edit made while the save is under way stays an unsaved edit", async () => {
    const gate = deferred<void>();
    const save = vi.fn(async (...args: Parameters<typeof customApi.save>) => {
      await gate.promise;
      return customApi.save(...args);
    });
    const s = make({ api: { ...customApi, save } });
    await s.open();
    s.edit("first");
    const done = s.save();
    s.edit("second");
    gate.resolve();
    await done;
    expect(mock.read("mine")).toBe("first");
    expect(s.state).toMatchObject({ buffer: "second", dirty: true });
    expect(readDraft(store, "mine", s.state.stored!)?.source).toBe("second");
  });

  it("does nothing while the file is loading or gone-and-not-imported", async () => {
    const s = make();
    await s.save();
    expect(mock.calls).toEqual([]);
  });
});

describe("run", () => {
  it("sends the unsaved buffer on the chart's query and delivers the preview; nothing is stored", async () => {
    const s = make();
    await s.open();
    s.edit('TITLE = "Draft"\n');
    await s.run(QUERY);
    const post = mock.calls.find((c) => c.path.endsWith("/preview"))!;
    expect(post.body).toMatchObject({ source: 'TITLE = "Draft"\n', id: "mine", ...QUERY });
    expect(previews).toHaveLength(1);
    expect(previews[0]).toMatchObject({ preview: true });
    // The preview is told which text it was made from: the chart asks for more bars with that text, not with whatever is typed by then.
    expect(previewSources).toEqual(['TITLE = "Draft"\n']);
    expect(mock.read("mine")).toBe(SRC);
    expect(s.state).toMatchObject({ running: false, error: null });
  });

  it("a source that raises shows its error with the line, and clears the preview", async () => {
    const s = make();
    await s.open();
    s.edit("a = 1\nRAISE\n");
    await s.run(QUERY);
    expect(s.state.error).toMatchObject({ type: "ZeroDivisionError", line: 2 });
    expect(previews.at(-1)).toBeNull();
  });

  it("the worker being off is said, and saving is still possible", async () => {
    mock.worker = false;
    const s = make();
    await s.open();
    s.edit("x");
    await s.run(QUERY);
    expect(s.state.error).toMatchObject({ message: "The indicator worker is not running." });
    await s.save();
    expect(mock.read("mine")).toBe("x");
  });

  it("makes no request for a text over the cap or when editing is off", async () => {
    const s = make({ allowed: false });
    await s.open();
    mock.calls.length = 0;
    await s.run(QUERY);
    expect(mock.calls).toEqual([]);
    const t = make();
    await t.open();
    mock.calls.length = 0;
    t.edit("a".repeat(MAX_SOURCE_BYTES + 1));
    await t.run(QUERY);
    expect(mock.calls).toEqual([]);
  });

  it("a newer run replaces an older one: the older answer is never shown, even if it arrives last", async () => {
    const gates = [deferred<PreviewValues>(), deferred<PreviewValues>()];
    let n = 0;
    const preview = vi.fn(() => gates[n++].promise);
    const s = make({ api: { ...customApi, preview } });
    await s.open();
    const first = s.run(QUERY);
    const second = s.run(QUERY);
    const old = { id: "old", preview: true } as unknown as PreviewValues;
    const fresh = { id: "fresh", preview: true } as unknown as PreviewValues;
    gates[1].resolve(fresh);
    await second;
    gates[0].resolve(old);
    await first;
    expect(previews).toEqual([fresh]);
    expect(s.state.running).toBe(false);
  });

  it("an older run's error does not replace a newer run's result", async () => {
    const gates = [deferred<PreviewValues>(), deferred<PreviewValues>()];
    let n = 0;
    const s = make({ api: { ...customApi, preview: vi.fn(() => gates[n++].promise) } });
    await s.open();
    const first = s.run(QUERY);
    const second = s.run(QUERY);
    gates[1].resolve({ id: "fresh", preview: true } as unknown as PreviewValues);
    await second;
    gates[0].reject(new Error("late failure"));
    await first;
    expect(s.state.error).toBeNull();
  });

  it("closing the session cancels a run in flight: its answer is not delivered", async () => {
    const gate = deferred<PreviewValues>();
    const s = make({ api: { ...customApi, preview: vi.fn(() => gate.promise) } });
    await s.open();
    const run = s.run(QUERY);
    s.close();
    gate.resolve({ id: "late", preview: true } as unknown as PreviewValues);
    await run;
    expect(previews.filter((p) => p !== null)).toEqual([]);
    expect(previews.at(-1)).toBeNull(); // closing removes the preview
  });
});

describe("when the file changes elsewhere", () => {
  it("a clean buffer takes the new text, with a note", async () => {
    const s = make();
    await s.open();
    mock.place("mine", "changed elsewhere");
    await s.refresh("save");
    expect(s.state).toMatchObject({ buffer: "changed elsewhere", dirty: false, notice: { kind: "remote" } });
    expect(s.state.stored?.version).toBe(mockVersion("changed elsewhere"));
  });

  it("an edited buffer is kept and the change is a conflict to settle at the next save", async () => {
    const s = make();
    await s.open();
    s.edit("my edit");
    mock.place("mine", "changed elsewhere");
    await s.refresh("save");
    expect(s.state).toMatchObject({ buffer: "my edit", dirty: true, conflict: { version: mockVersion("changed elsewhere") }, notice: { kind: "conflict" } });
  });

  it("our own save's broadcast is not a change", async () => {
    const s = make();
    await s.open();
    s.edit("mine now");
    await s.save();
    await s.refresh("save");
    expect(s.state.notice).toMatchObject({ kind: "saved" });
  });

  it("a delete makes the file 'gone' and keeps the buffer", async () => {
    const s = make();
    await s.open();
    s.edit("my edit");
    mock.files.delete("mine");
    await s.refresh("delete");
    expect(s.state).toMatchObject({ phase: "gone", buffer: "my edit" });
  });

  it("a failed read leaves everything as it was", async () => {
    const source = vi.fn().mockResolvedValueOnce(await customApi.source("mine")).mockRejectedValue(new Error("down"));
    const s = make({ api: { ...customApi, source } });
    await s.open();
    s.edit("my edit");
    await s.refresh("save");
    expect(s.state).toMatchObject({ phase: "ready", buffer: "my edit", dirty: true });
  });
});

describe("versions", () => {
  async function opened() {
    const s = make();
    await s.open();
    s.edit("v = 2\n");
    await s.save();
    s.edit("v = 3\n");
    await s.save();
    return s;
  }

  it("lists them newest first", async () => {
    const s = await opened();
    await s.loadVersions();
    expect(s.state.versions).toHaveLength(2);
    await s.view(s.state.versions![0].version);
    expect(s.state.viewing?.source).toBe("v = 2\n");
  });

  it("viewing one does not touch the buffer; closing the view shows the buffer again", async () => {
    const s = await opened();
    s.edit("typing");
    await s.loadVersions();
    await s.view(s.state.versions![1].version);
    expect(s.state.buffer).toBe("typing");
    s.closeView();
    expect(s.state.viewing).toBeNull();
  });

  it("restore is a save: the buffer and the stored text become that version, and the current one is a new version", async () => {
    const s = await opened();
    await s.loadVersions();
    const target = s.state.versions![1];
    await s.view(target.version);
    const text = s.state.viewing!.source;
    await s.restoreVersion(target.version);
    expect(mock.read("mine")).toBe(text);
    expect(s.state).toMatchObject({ buffer: text, dirty: false, viewing: null, notice: { kind: "restored" } });
    expect(s.state.versions!.length).toBe(3);
  });

  it("restore with a stale base is a conflict and changes nothing", async () => {
    const s = await opened();
    await s.loadVersions();
    mock.place("mine", "changed elsewhere");
    await s.restoreVersion(s.state.versions![0].version);
    expect(mock.read("mine")).toBe("changed elsewhere");
    expect(s.state.conflict).not.toBeNull();
  });

  it("a version that is not there is said, not thrown", async () => {
    const s = await opened();
    await s.view("20200101T000000Z");
    expect(s.state.viewing).toBeNull();
    expect(s.state.notice).toMatchObject({ kind: "failed" });
  });
});

describe("leaving", () => {
  it("is free when nothing is unsaved and asks when something is", async () => {
    const s = make();
    await s.open();
    expect(s.hasUnsaved()).toBe(false);
    s.edit("x");
    expect(s.hasUnsaved()).toBe(true);
    await s.save();
    expect(s.hasUnsaved()).toBe(false);
  });

  it("a gone file with a buffer still counts as unsaved", async () => {
    const s = make();
    await s.open();
    s.edit("x");
    mock.files.delete("mine");
    await s.save();
    expect(s.hasUnsaved()).toBe(true);
  });
});
