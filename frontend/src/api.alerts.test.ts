// @vitest-environment happy-dom
import { afterEach, describe, expect, it, vi } from "vitest";

import { alertsApi, customApi, layoutsApi } from "./api";

function fetchSpy(body: unknown = {}) {
  const calls: Array<{ url: string; method: string; body: unknown }> = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: URL, init: RequestInit) => {
      calls.push({ url: `${url.pathname}${url.search}`, method: init.method ?? "GET", body: init.body ? JSON.parse(String(init.body)) : undefined });
      return new Response(JSON.stringify(body), { status: 200, headers: { "Content-Type": "application/json" } });
    }),
  );
  return calls;
}
afterEach(() => vi.unstubAllGlobals());

describe("the calls the indicator alert screens make", () => {
  it("applying a layout names each alert it will stop, one stop_alerts each", async () => {
    const calls = fetchSpy();
    await layoutsApi.apply("L1", "EUR_USD", ["a", "b"]);
    expect(calls[0]).toMatchObject({ method: "POST", url: "/api/layouts/L1/apply?instrument=EUR_USD&stop_alerts=a&stop_alerts=b" });
  });

  it("applying with no alert to stop sends none", async () => {
    const calls = fetchSpy();
    await layoutsApi.apply("L1", "EUR_USD");
    expect(calls[0].url).toBe("/api/layouts/L1/apply?instrument=EUR_USD");
  });

  it("the preview is a GET that changes nothing", async () => {
    const calls = fetchSpy({ instrument: "EUR_USD", alerts_stopped: [], alerts_rearmed: [] });
    await layoutsApi.applyPreview("L1", "EUR_USD");
    expect(calls[0]).toMatchObject({ method: "GET", url: "/api/layouts/L1/apply-preview?instrument=EUR_USD" });
  });

  it("deleting a definition names the alerts, one alerts= each", async () => {
    const calls = fetchSpy();
    await customApi.remove("mine", true, ["a", "b"]);
    await customApi.remove("mine");
    expect(calls.map((c) => c.url)).toEqual(["/api/custom-indicators/mine?confirm=true&alerts=a&alerts=b", "/api/custom-indicators/mine"]);
  });

  it("an indicator alert goes out with its indicator part", async () => {
    const calls = fetchSpy();
    await alertsApi.create({
      instrument: "EUR_USD",
      type: "indicator",
      timeframe: "1h",
      indicator: { uid: "u1", output: "rsi", condition: "cross_value", value: 30 },
    });
    expect(calls[0].body).toEqual({ instrument: "EUR_USD", type: "indicator", timeframe: "1h", indicator: { uid: "u1", output: "rsi", condition: "cross_value", value: 30 } });
  });
});
