// @vitest-environment happy-dom
import { beforeEach, describe, expect, it } from "vitest";

import type { Alert } from "../api";
import { confirmRemoveInstance } from "./removeInstance";

const A = (id: string, status: Alert["status"] = "active") => ({ id, name: `alert ${id}`, status, state: { error: null } }) as unknown as Alert;
const btn = (t: string) => [...document.querySelectorAll("button")].find((b) => b.textContent === t) as HTMLButtonElement;
beforeEach(() => document.body.replaceChildren());

describe("removing an indicator from the chart", () => {
  it("removes at once when no alert runs on it", async () => {
    expect(await confirmRemoveInstance("RSI(14)", [A("a", "stopped")])).toBe(true);
    expect(document.querySelector("[role=alertdialog]")).toBeNull();
  });

  it("names the alerts that will stop, and waits for the person", async () => {
    const p = confirmRemoveInstance("RSI(14)", [A("a"), A("b")]);
    expect(document.querySelector(".confirm-text")?.textContent).toBe("Removing RSI(14) stops 2 alerts. They stay in the Alerts panel, stopped, with their settings.");
    expect([...document.querySelectorAll(".confirm-list li")].map((l) => l.textContent)).toEqual(["alert a", "alert b"]);
    btn("Remove and stop 2 alerts").click();
    expect(await p).toBe(true);
  });

  it("Cancel keeps the indicator", async () => {
    const p = confirmRemoveInstance("RSI(14)", [A("a")]);
    btn("Cancel").click();
    expect(await p).toBe(false);
  });
});
