import { describe, expect, it } from "vitest";
import { warmupNotice } from "./notice";

const CUT = {
  kind: "warmup_cut",
  needed: 20010,
  limit: 20000,
  message: "These settings need 20,010 bars of earlier history to settle, but only 20,000 are used.",
};

describe("the warmup notice in the legend", () => {
  it("is a small warning with the server's message as its tip", () => {
    expect(warmupNotice(CUT)).toEqual({ label: "Warmup cut", tip: CUT.message });
  });

  it("is absent when the server sent none", () => {
    expect(warmupNotice(undefined)).toBeNull();
    expect(warmupNotice(null)).toBeNull();
  });

  it("ignores anything it does not know, rather than showing a half-formed warning", () => {
    expect(warmupNotice({ ...CUT, kind: "other" })).toBeNull();
    expect(warmupNotice({ kind: "warmup_cut", needed: 5 })).toBeNull();
    expect(warmupNotice({ ...CUT, message: "" })).toBeNull();
    expect(warmupNotice("warmup_cut")).toBeNull();
  });

  it("falls back to the numbers when the message is missing but they are there", () => {
    const { message: _drop, ...bare } = CUT;
    expect(warmupNotice(bare)?.tip).toContain("20,010");
  });
});
