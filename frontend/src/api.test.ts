import { describe, expect, it } from "vitest";

import { ApiError, CLIENT_API_VERSION, apiErrorFrom, chartPath, definitionPath } from "./api";
import serverInit from "../../src/openchart/__init__.py?raw";

describe("the API version", () => {
  it("is the one the server reports (src/openchart/__init__.py), so a page never warns about its own server", () => {
    expect(Number(/^API_VERSION = (\d+)$/m.exec(serverInit)?.[1])).toBe(CLIENT_API_VERSION);
  });
});

describe("paths of the CI-3 routes", () => {
  it("name the chart by source and instrument, each encoded as one segment", () => {
    expect(chartPath("oanda", "XAU_USD")).toBe("api/charts/oanda/XAU_USD/indicators");
    expect(chartPath("a/b", "EUR USD")).toBe("api/charts/a%2Fb/EUR%20USD/indicators");
  });

  it("name a definition by its id, encoded", () => {
    expect(definitionPath("sma_cross")).toBe("api/custom-indicators/sma_cross");
    expect(definitionPath("a/../b")).toBe("api/custom-indicators/a%2F..%2Fb");
  });
});

describe("apiErrorFrom", () => {
  it("uses a string detail as the message", () => {
    const e = apiErrorFrom(404, "Not Found", { detail: "no indicator of yours named 'x'" });
    expect(e).toBeInstanceOf(ApiError);
    expect([e.status, e.message, e.detail]).toEqual([404, "no indicator of yours named 'x'", "no indicator of yours named 'x'"]);
  });

  it("uses the message of an object detail and keeps the object (a 409 with its usage)", () => {
    const detail = { message: '"SMA" is on 2 charts. Deleting it removes it from all of them.', usage: { id: "sma", in_use: true } };
    const e = apiErrorFrom(409, "Conflict", { detail });
    expect(e.status).toBe(409);
    expect(e.message).toBe(detail.message);
    expect(e.detail).toEqual(detail);
  });

  it("uses the first message of a validation error list", () => {
    const e = apiErrorFrom(422, "Unprocessable Entity", { detail: [{ msg: "field required", loc: ["body"] }, { msg: "other" }] });
    expect(e.message).toBe("field required");
  });

  it("does not take an empty text for a message", () => {
    expect(apiErrorFrom(400, "Bad Request", { detail: "" }).message).toBe("Bad Request");
    expect(apiErrorFrom(409, "Conflict", { detail: { message: "" } }).message).toBe("Conflict");
    expect(apiErrorFrom(422, "Unprocessable", { detail: [{ msg: "" }] }).message).toBe("Unprocessable");
  });

  it("falls back to the status text when the body has nothing usable", () => {
    expect(apiErrorFrom(500, "Internal Server Error", null).message).toBe("Internal Server Error");
    expect(apiErrorFrom(500, "Internal Server Error", {}).message).toBe("Internal Server Error");
    expect(apiErrorFrom(409, "Conflict", { detail: { no: "message" } }).message).toBe("Conflict");
    expect(apiErrorFrom(422, "Unprocessable", { detail: [] }).message).toBe("Unprocessable");
  });
});
