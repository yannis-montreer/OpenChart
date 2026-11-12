// @vitest-environment happy-dom
import { beforeEach, describe, expect, it } from "vitest";

import { confirmWithAlerts } from "./alertsConfirm";

const ask = () =>
  confirmWithAlerts({ title: "Apply “Gold H1”?", text: "Applying stops 2 indicator alerts.", lines: ["RSI under 30 (EUR_USD)", "EMA cross"], confirm: "Apply and stop 2 alerts", danger: true });
const btn = (t: string) => [...document.querySelectorAll("button")].find((b) => b.textContent === t) as HTMLButtonElement;
beforeEach(() => document.body.replaceChildren());

describe("a confirmation that lists the alerts", () => {
  it("says what will happen and lists each alert", () => {
    void ask();
    expect(document.querySelector(".confirm-text")?.textContent).toBe("Applying stops 2 indicator alerts.");
    expect([...document.querySelectorAll(".confirm-list li")].map((l) => l.textContent)).toEqual(["RSI under 30 (EUR_USD)", "EMA cross"]);
    expect(document.querySelector("[role=alertdialog]")?.getAttribute("aria-label")).toBe("Apply “Gold H1”?");
  });

  it("resolves true only on the confirming button", async () => {
    const p = ask();
    btn("Apply and stop 2 alerts").click();
    expect(await p).toBe(true);
    expect(document.querySelector(".confirm-backdrop")).toBeNull();
  });

  it("resolves false on Cancel, on Escape and on the backdrop; the safe button has the focus", async () => {
    let p = ask();
    expect(document.activeElement).toBe(btn("Cancel"));
    btn("Cancel").click();
    expect(await p).toBe(false);
    p = ask();
    document.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape" }));
    expect(await p).toBe(false);
    p = ask();
    document.querySelector(".confirm-backdrop")!.dispatchEvent(new Event("pointerdown", { bubbles: true }));
    expect(await p).toBe(false);
  });
});
