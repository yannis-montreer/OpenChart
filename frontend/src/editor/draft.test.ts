import { describe, expect, it } from "vitest";

import { dropDraft, readDraft, syncDraft, type DraftStore } from "./draft";
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

const STORED = { version: "aaaaaaaaaaaa", source: "NAME = 'x'\n" };

describe("syncDraft", () => {
  it("keeps what differs from the stored file, with the version it started from", () => {
    const store = new MemoryStore();
    syncDraft(store, "mine", "NAME = 'y'\n", STORED, 1000);
    expect(readDraft(store, "mine", STORED)).toEqual({ base: "aaaaaaaaaaaa", source: "NAME = 'y'\n", at: 1000 });
  });

  it("keeps none when the buffer is the stored file (an edit undone, or a save)", () => {
    const store = new MemoryStore();
    syncDraft(store, "mine", "changed", STORED, 1);
    syncDraft(store, "mine", STORED.source, STORED, 2);
    expect(store.data.size).toBe(0);
  });

  it("never writes for a name that is not an id", () => {
    const store = new MemoryStore();
    for (const id of ["../x", "Mine", "", "a b", "mine\n"]) syncDraft(store, id, "x", STORED, 1);
    expect(store.data.size).toBe(0);
  });

  it("does not keep a text over the cap", () => {
    const store = new MemoryStore();
    syncDraft(store, "mine", "a".repeat(MAX_SOURCE_BYTES + 1), STORED, 1);
    expect(store.data.size).toBe(0);
  });

  it("survives a store that is missing, blocked or full", () => {
    expect(() => syncDraft(null, "mine", "x", STORED, 1)).not.toThrow();
    const full: DraftStore = {
      getItem: () => null,
      setItem: () => {
        throw new DOMException("full", "QuotaExceededError");
      },
      removeItem: () => {
        throw new Error("blocked");
      },
    };
    expect(() => syncDraft(full, "mine", "x", STORED, 1)).not.toThrow();
    expect(() => syncDraft(full, "mine", STORED.source, STORED, 1)).not.toThrow();
    expect(() => dropDraft(full, "mine")).not.toThrow();
    expect(readDraft(full, "mine", STORED)).toBeNull();
  });
});

describe("readDraft", () => {
  it("offers a draft only while the stored file is still the one it started from", () => {
    const store = new MemoryStore();
    syncDraft(store, "mine", "mine edit", STORED, 5);
    expect(readDraft(store, "mine", STORED)).not.toBeNull();
    // The file changed elsewhere: the draft is stale, gone and never offered.
    expect(readDraft(store, "mine", { version: "bbbbbbbbbbbb", source: "other" })).toBeNull();
    expect(store.data.size).toBe(0);
  });

  it("does not offer a draft that equals the stored text", () => {
    const store = new MemoryStore();
    store.setItem("openchart.draft.v1:mine", JSON.stringify({ base: STORED.version, source: STORED.source, at: 1 }));
    expect(readDraft(store, "mine", STORED)).toBeNull();
  });

  it("treats damaged entries as none, and removes them", () => {
    const store = new MemoryStore();
    for (const bad of ["not json", "[]", "null", '{"base":1,"source":"x","at":1}', '{"base":"a","source":2,"at":1}', '{"base":"a","source":"x"}']) {
      store.setItem("openchart.draft.v1:mine", bad);
      expect(readDraft(store, "mine", STORED), bad).toBeNull();
      expect(store.data.size, bad).toBe(0);
    }
  });

  it("does not offer a draft of the right version with a wrong shape", () => {
    const store = new MemoryStore();
    for (const bad of [
      { base: STORED.version, source: "x" }, // no time
      { base: STORED.version, source: 5, at: 1 },
      { base: STORED.version, source: "x", at: "1" },
    ]) {
      store.setItem("openchart.draft.v1:mine", JSON.stringify(bad));
      expect(readDraft(store, "mine", STORED), JSON.stringify(bad)).toBeNull();
    }
  });

  it("does not offer a draft the server would refuse", () => {
    const store = new MemoryStore();
    store.setItem("openchart.draft.v1:mine", JSON.stringify({ base: STORED.version, source: "a\u0000b", at: 1 }));
    expect(readDraft(store, "mine", STORED)).toBeNull();
  });

  it("keeps each file's draft apart", () => {
    const store = new MemoryStore();
    syncDraft(store, "a", "A edit", STORED, 1);
    syncDraft(store, "b", "B edit", STORED, 2);
    expect(readDraft(store, "a", STORED)?.source).toBe("A edit");
    expect(readDraft(store, "b", STORED)?.source).toBe("B edit");
    dropDraft(store, "a");
    expect(readDraft(store, "a", STORED)).toBeNull();
    expect(readDraft(store, "b", STORED)?.source).toBe("B edit");
  });
});
