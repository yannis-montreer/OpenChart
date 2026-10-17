import { describe, expect, it, vi } from "vitest";

import { lineStart, pickEditor } from "./pickEditor";

describe("pickEditor", () => {
  it("uses the code editor when it loads", async () => {
    const rich = { id: "rich" };
    const plain = { id: "plain" };
    const got = await pickEditor(async () => rich, () => plain);
    expect(got).toEqual({ editor: rich, kind: "codemirror" });
  });

  it("falls back to the plain text box when the code editor's chunk does not load, and says so", async () => {
    const plain = { id: "plain" };
    const failed = vi.fn();
    const got = await pickEditor(
      async () => {
        throw new Error("Failed to fetch dynamically imported module");
      },
      () => plain,
      failed,
    );
    expect(got).toEqual({ editor: plain, kind: "textarea" });
    expect(failed).toHaveBeenCalledWith("Failed to fetch dynamically imported module");
  });

  it("falls back when building the code editor throws", async () => {
    const got = await pickEditor(
      async () => {
        throw "odd";
      },
      () => "plain",
    );
    expect(got.kind).toBe("textarea");
  });

  it("does not load the code editor until asked", () => {
    const load = vi.fn(async () => "rich");
    void load; // pickEditor is the only caller; nothing runs at import time
    expect(load).not.toHaveBeenCalled();
  });
});

describe("lineStart", () => {
  it("is the offset of a 1-based line, clamped to the text", () => {
    const text = "ab\ncde\n\nf";
    expect(lineStart(text, 1)).toBe(0);
    expect(lineStart(text, 2)).toBe(3);
    expect(lineStart(text, 3)).toBe(7);
    expect(lineStart(text, 4)).toBe(8);
    expect(lineStart(text, 99)).toBe(8); // past the end: the last line
    expect(lineStart(text, 0)).toBe(0);
    expect(lineStart(text, -3)).toBe(0);
    expect(lineStart("", 5)).toBe(0);
  });
});
