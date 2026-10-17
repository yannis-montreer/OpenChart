import { describe, expect, it } from "vitest";

import {
  MAX_SOURCE_BYTES,
  fileProblem,
  idFromFilename,
  idProblem,
  isId,
  normalizeIdInput,
  sourceBytes,
  sourceProblem,
  titleProblem,
} from "./rules";

describe("isId", () => {
  it.each(["a", "sma_cross", "x9", "a".repeat(40)])("accepts %s", (id) => expect(isId(id)).toBe(true));
  it.each([
    "",
    "Mine",
    "9lives",
    "_mine",
    "mine\n",
    "mine ",
    "a.b",
    "a/b",
    "a\\b",
    "..",
    "a".repeat(41),
    "é",
    "mine%0A",
  ])("refuses %j", (id) => expect(isId(id)).toBe(false));
});

describe("idProblem", () => {
  it("is null for an id and says what is wrong otherwise", () => {
    expect(idProblem("sma_cross")).toBeNull();
    expect(idProblem("")).toBe("Give it a name.");
    expect(idProblem("a".repeat(41))).toBe("At most 40 characters.");
    expect(idProblem("9lives")).toBe("Start with a lowercase letter.");
    expect(idProblem("My RSI")).toBe("Start with a lowercase letter.");
    expect(idProblem("my rsi")).toBe("Use only lowercase letters, digits and _.");
    expect(idProblem("mine\n")).toBe("Use only lowercase letters, digits and _.");
  });
});

describe("normalizeIdInput", () => {
  it("trims and lowercases, and changes nothing else", () => {
    expect(normalizeIdInput("  Sma_Cross ")).toBe("sma_cross");
    expect(normalizeIdInput("My RSI")).toBe("my rsi"); // still refused by idProblem
    expect(normalizeIdInput("../Up")).toBe("../up");
  });
});

describe("idFromFilename", () => {
  it("follows the contract's rule for an import", () => {
    expect(idFromFilename("My RSI.py")).toBe("my_rsi");
    expect(idFromFilename("sma_cross.py")).toBe("sma_cross");
    expect(idFromFilename("9lives.py")).toBe("x9lives");
    expect(idFromFilename("_hidden.PY")).toBe("x_hidden");
    expect(idFromFilename("a-b.c.py")).toBe("a_b_c");
    expect(idFromFilename("é.py")).toBe("x_");
    expect(idFromFilename("a".repeat(60) + ".py")).toBe("a".repeat(40));
    expect(idFromFilename(".py")).toBe("imported");
    expect(idFromFilename("")).toBe("imported");
  });

  it("always makes an id", () => {
    for (const name of ["", "..", "../x.py", "C:\\dir\\Z Z.py", "9", "_", "\n.py", "😀.py", "A".repeat(100)]) {
      expect(isId(idFromFilename(name)), name).toBe(true);
    }
  });

  it("uses the last path segment only", () => {
    expect(idFromFilename("C:\\Users\\me\\My RSI.py")).toBe("my_rsi");
    expect(idFromFilename("a/b/c.py")).toBe("c");
  });
});

describe("sourceBytes and sourceProblem", () => {
  it("counts UTF-8 bytes, not characters", () => {
    expect(sourceBytes("abc")).toBe(3);
    expect(sourceBytes("é")).toBe(2);
    expect(sourceBytes("€")).toBe(3);
    expect(sourceBytes("😀")).toBe(4);
  });

  it("allows exactly 256 KB and refuses one byte more", () => {
    expect(MAX_SOURCE_BYTES).toBe(262144);
    expect(sourceProblem("a".repeat(MAX_SOURCE_BYTES))).toBeNull();
    expect(sourceProblem("a".repeat(MAX_SOURCE_BYTES + 1))).toMatch(/256 KB/);
  });

  it("counts a multi-byte file by its bytes", () => {
    const fits = "é".repeat(MAX_SOURCE_BYTES / 2);
    expect(sourceProblem(fits)).toBeNull();
    expect(sourceProblem(`${fits}a`)).toMatch(/256 KB/);
  });

  it("refuses NUL and lone surrogates like the server", () => {
    expect(sourceProblem("a\u0000b")).toBe("The code holds a NUL character.");
    expect(sourceProblem("a\ud800b")).toBe("The code is not valid Unicode text.");
    expect(sourceProblem("a\udc00")).toBe("The code is not valid Unicode text.");
    expect(sourceProblem("😀")).toBeNull();
  });

  it("accepts an empty file (it saves, and shows its load error)", () => {
    expect(sourceProblem("")).toBeNull();
  });
});

describe("titleProblem", () => {
  it("wants 1 to 60 characters on one line", () => {
    expect(titleProblem("SMA cross")).toBeNull();
    expect(titleProblem("a".repeat(60))).toBeNull();
    expect(titleProblem("😀".repeat(60))).toBeNull();
    expect(titleProblem("a".repeat(61))).toBe("At most 60 characters.");
    expect(titleProblem("")).toBe("Give it a title.");
    expect(titleProblem("   ")).toBe("Give it a title.");
    expect(titleProblem("a\nb")).toBe("A title is one line.");
    expect(titleProblem("a\rb")).toBe("A title is one line.");
  });
});

describe("fileProblem", () => {
  it("refuses a file that is not .py or is over the cap, before it is read", () => {
    expect(fileProblem({ name: "x.py", size: 10 })).toBeNull();
    expect(fileProblem({ name: "X.PY", size: MAX_SOURCE_BYTES })).toBeNull();
    expect(fileProblem({ name: "x.py", size: MAX_SOURCE_BYTES + 1 })).toMatch(/256 KB/);
    expect(fileProblem({ name: "x.exe", size: 10 })).toBe("Pick a .py file.");
    expect(fileProblem({ name: "x", size: 10 })).toBe("Pick a .py file.");
  });
});
