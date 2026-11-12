// @vitest-environment happy-dom
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { ApiError, customApi, type DefinitionUsage } from "../api";
import { openDeleteDefinition } from "./deleteDefinition";

const USAGE: DefinitionUsage = {
  id: "mine",
  in_use: true,
  charts: [{ source: "oanda", instrument: "EUR_USD", instances: 1 }],
  layouts: [],
  alerts: [
    { id: "a", name: "RSI under 30", instrument: "EUR_USD", uid: "u1", status: "active" },
    { id: "b", name: "old one", instrument: "EUR_USD", uid: "u1", status: "stopped" },
  ],
};
const tick = () => new Promise((r) => setTimeout(r, 0));
const btn = (t: string) => [...document.querySelectorAll("button")].find((b) => b.textContent === t) as HTMLButtonElement;
beforeEach(() => document.body.replaceChildren());
afterEach(() => vi.restoreAllMocks());

describe("deleting a definition that has alerts", () => {
  it("lists the alerts, and the button says how many stop", async () => {
    vi.spyOn(customApi, "usage").mockResolvedValue(USAGE);
    openDeleteDefinition({ id: "mine", title: "Mine", onChanged: vi.fn() });
    await tick();
    const headings = [...document.querySelectorAll(".ind-usage b")].map((b) => b.textContent);
    expect(headings).toEqual(["Charts", "Alerts"]);
    expect([...document.querySelectorAll(".ind-usage li")].map((l) => l.textContent)).toEqual(["EUR_USD (1 indicator)", "RSI under 30 (EUR_USD)", "old one (EUR_USD, stopped)"]);
    expect(btn("Delete and stop 1 alert")).toBeTruthy();
  });

  it("names every listed alert in the delete", async () => {
    vi.spyOn(customApi, "usage").mockResolvedValue(USAGE);
    const remove = vi.spyOn(customApi, "remove").mockResolvedValue({ id: "mine", moved_to: ".deleted/x", removed: USAGE });
    openDeleteDefinition({ id: "mine", title: "Mine", onChanged: vi.fn() });
    await tick();
    btn("Delete and stop 1 alert").click();
    await tick();
    expect(remove).toHaveBeenCalledWith("mine", true, ["a", "b"]);
  });

  it("when an alert appeared meanwhile, shows the new list and asks again instead of deleting", async () => {
    vi.spyOn(customApi, "usage").mockResolvedValue(USAGE);
    const fresh = { ...USAGE, alerts: [...USAGE.alerts!, { id: "c", name: "new alert", instrument: "XAU_USD", uid: "u9", status: "active" }] };
    const err = new ApiError(409, "Not named: c.");
    err.detail = { message: "Not named: c.", usage: fresh };
    const remove = vi.spyOn(customApi, "remove").mockRejectedValueOnce(err);
    openDeleteDefinition({ id: "mine", title: "Mine", onChanged: vi.fn() });
    await tick();
    btn("Delete and stop 1 alert").click();
    await tick();
    expect(document.querySelector(".confirm-text")?.textContent).toBe("Not named: c.");
    expect([...document.querySelectorAll(".ind-usage li")].map((l) => l.textContent)).toContain("new alert (XAU_USD)");
    expect(remove).toHaveBeenCalledTimes(1);
    expect(btn("Delete and stop 2 alerts")).toBeTruthy();
  });
});
