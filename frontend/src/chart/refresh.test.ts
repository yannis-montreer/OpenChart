import { describe, expect, it } from "vitest";

import { RETRY_MS, nextRefresh, versionChanged } from "./refresh";

describe("a new version of an indicator's file", () => {
  it("is noticed when the answer names another version than the one held", () => {
    expect(versionChanged("3f2a9c1d4e5b", "9a8b7c6d5e4f")).toBe(true);
  });

  it("is not there on the first answer, or when it is the same", () => {
    expect(versionChanged(undefined, "3f2a9c1d4e5b")).toBe(false);
    expect(versionChanged("3f2a9c1d4e5b", "3f2a9c1d4e5b")).toBe(false);
  });

  it("is not guessed from an answer without a version (built-in indicators have none)", () => {
    expect(versionChanged("3f2a9c1d4e5b", undefined)).toBe(false);
    expect(versionChanged(undefined, undefined)).toBe(false);
  });
});

describe("what the once-a-second refresh does", () => {
  it("refreshes the last bars when all is well", () => {
    expect(nextRefresh({ error: false, lastTry: 0 }, 1_000_000)).toBe("tail");
  });

  it("tries a full load again after an error, but not more often than every few seconds", () => {
    const t0 = 1_000_000;
    expect(nextRefresh({ error: true, lastTry: t0 }, t0 + 1000)).toBe("wait");
    expect(nextRefresh({ error: true, lastTry: t0 }, t0 + RETRY_MS - 1)).toBe("wait");
    expect(nextRefresh({ error: true, lastTry: t0 }, t0 + RETRY_MS)).toBe("reload");
  });

  it("an indicator that has never failed is refreshed whatever lastTry says", () => {
    expect(nextRefresh({ error: false, lastTry: 999_999 }, 1_000_000)).toBe("tail");
  });
});
