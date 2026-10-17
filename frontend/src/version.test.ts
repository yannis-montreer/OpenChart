import { describe, expect, it } from "vitest";

import { versionNotice } from "./version";

describe("the API version this page and the server speak", () => {
  it("says nothing when they match", () => {
    expect(versionNotice(2, 2)).toBeNull();
  });

  it("asks for a reload when the server is newer", () => {
    expect(versionNotice(1, 2)).toBe("This page is older than the server (API 1 vs 2). Reload to update.");
  });

  it("says what stops working when the server is older (the indicator lists live on the server since API 2)", () => {
    expect(versionNotice(2, 1)).toBe("This server is older than this page (API 2 vs 1). Update OpenChart on the server: indicators cannot be saved until then.");
  });
});
