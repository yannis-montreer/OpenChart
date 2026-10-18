import { describe, expect, it } from "vitest";

import { ApiError } from "../api";
import { errorFromApi, errorOf, findingsOf, pieces } from "./errors";

describe("errorOf", () => {
  it("reads the error a save or a preview answers", () => {
    expect(errorOf({ type: "SyntaxError", message: "invalid syntax", line: 23, traceback: "  line 23\n    up = 1 /" })).toEqual({
      type: "SyntaxError",
      message: "invalid syntax",
      line: 23,
      traceback: "  line 23\n    up = 1 /",
    });
  });

  it("is null for no error", () => {
    expect(errorOf(null)).toBeNull();
    expect(errorOf(undefined)).toBeNull();
  });

  it("makes one of a plain text", () => {
    expect(errorOf("The indicator worker is not running.")).toEqual({
      type: "Error",
      message: "The indicator worker is not running.",
      line: null,
      traceback: null,
    });
  });

  it("does not trust shapes: a missing or wrong field becomes null or a default", () => {
    expect(errorOf({ type: 5, message: ["x"], line: "3", traceback: {} })).toEqual({ type: "Error", message: "", line: null, traceback: null });
    expect(errorOf({ message: "m", line: 0 })).toMatchObject({ line: null });
    expect(errorOf({ message: "m", line: -2 })).toMatchObject({ line: null });
    expect(errorOf({ message: "m", line: 1.5 })).toMatchObject({ line: null });
    expect(errorOf(42)).toBeNull();
    expect(errorOf([])).toBeNull();
  });
});

describe("errorFromApi", () => {
  it("takes a 422 detail object", () => {
    const err = new ApiError(422, "boom");
    err.detail = { type: "ZeroDivisionError", message: "division by zero", line: 7, traceback: "line 7" };
    expect(errorFromApi(err)).toMatchObject({ type: "ZeroDivisionError", line: 7 });
  });

  it("takes a text detail as the message", () => {
    const err = new ApiError(503, "The indicator worker is not running.");
    err.detail = "The indicator worker is not running.";
    expect(errorFromApi(err)).toEqual({ type: "Error", message: "The indicator worker is not running.", line: null, traceback: null });
  });

  it("falls back to the error's own message, and to a plain Error for anything else", () => {
    expect(errorFromApi(new ApiError(500, "Internal Server Error"))).toMatchObject({ message: "Internal Server Error" });
    expect(errorFromApi(new Error("Failed to fetch"))).toMatchObject({ message: "Failed to fetch" });
    expect(errorFromApi("odd")).toMatchObject({ message: "odd" });
  });

  it("does not take a validation list for a traceback", () => {
    const err = new ApiError(422, "Field required");
    err.detail = [{ msg: "Field required", loc: ["body", "source"] }];
    expect(errorFromApi(err)).toEqual({ type: "Error", message: "Field required", line: null, traceback: null });
  });
});

describe("pieces", () => {
  it("turns 'line N' into a link piece when the line exists", () => {
    expect(pieces("File x.py, line 3, in compute", 10)).toEqual([{ text: "File x.py, " }, { text: "line 3", line: 3 }, { text: ", in compute" }]);
  });

  it("leaves a number outside the file as text", () => {
    expect(pieces("line 0 and line 11 and line 10", 10)).toEqual([{ text: "line 0 and line 11 and " }, { text: "line 10", line: 10 }]);
  });

  it("is one text piece when there is no line", () => {
    expect(pieces("nothing here", 5)).toEqual([{ text: "nothing here" }]);
    expect(pieces("", 5)).toEqual([]);
  });

  it("never makes a link of anything but a line number", () => {
    for (const text of ["see https://example.com/line 3", "<a href=x>line 2</a>", "javascript:alert(1)"]) {
      for (const p of pieces(text, 9)) expect(Object.keys(p).sort()).toEqual(expect.arrayContaining(["text"]));
      expect(pieces(text, 9).map((p) => p.text).join("")).toBe(text);
    }
  });

  it("handles a file of no lines and huge numbers", () => {
    expect(pieces("line 1", 0)).toEqual([{ text: "line 1" }]);
    expect(pieces("line 99999999999999999999", 10)).toEqual([{ text: "line 99999999999999999999" }]);
  });
});

describe("findingsOf", () => {
  it("lists the warnings and notes of every check of the current version", () => {
    const checks = [
      {
        version: "abc",
        lookahead: "warned",
        warmup: "ok",
        findings: [
          { check: "lookahead", level: "warning", output: "x", message: "'x' changes when the last 50 bars are removed." },
          { check: "warmup", level: "note", output: null, message: "Checked with 5 other settings." },
        ],
      },
    ];
    expect(findingsOf(checks, "abc")).toEqual([
      { level: "warning", text: "Lookahead: 'x' changes when the last 50 bars are removed." },
      { level: "note", text: "Warmup: Checked with 5 other settings." },
    ]);
  });

  it("ignores the checks of another version and anything that is not a list", () => {
    expect(findingsOf([{ version: "old", findings: [{ check: "lookahead", level: "warning", message: "m" }] }], "new")).toEqual([]);
    expect(findingsOf(undefined, "x")).toEqual([]);
    expect(findingsOf({ version: "x" }, "x")).toEqual([]);
    expect(findingsOf([null, 3, { version: "x", findings: "no" }], "x")).toEqual([]);
  });

  it("does not repeat a finding that two settings share", () => {
    const f = { check: "lookahead", level: "warning", message: "same" };
    expect(findingsOf([{ version: "v", findings: [f] }, { version: "v", findings: [f] }], "v")).toHaveLength(1);
  });
});
