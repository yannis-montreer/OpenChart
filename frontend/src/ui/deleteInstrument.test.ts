// @vitest-environment happy-dom
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { account, type DeletePreview } from "../api";
import { deleteInstrumentDialog } from "./deleteInstrument";

const PREVIEW: DeletePreview = {
  instrument: "EUR_DKK",
  stored: { bytes: 1024, candles: 10, closures: 0, tick_files: 0, tick_bytes: 0, drawings: 1, alerts: 5, active_alerts: 3, indicator_alerts: 2, indicators: 1, m1: 5, h1: 5, cached: 0 },
  needs_drawings_and_alerts: true,
  token: "t",
};
const tick = () => new Promise((r) => setTimeout(r, 0));
beforeEach(() => document.body.replaceChildren());
afterEach(() => vi.restoreAllMocks());

describe("Delete data… dialog", () => {
  it("shows how many of the alerts are on indicators, before and after ticking the box", async () => {
    vi.spyOn(account, "deletePreview").mockResolvedValue(PREVIEW);
    void deleteInstrumentDialog("EUR_DKK");
    await tick();
    const lines = () => [...document.querySelectorAll(".delete-list li")].map((l) => l.textContent);
    expect(lines().at(-1)).toBe("Kept unless the box below is ticked: 1 drawing and 5 alerts (2 on indicators)");
    expect(document.querySelector(".delete-both")?.textContent).toBe("Also delete its 1 drawing and 5 alerts (2 on indicators)");
    const box = document.querySelector(".delete-both input") as HTMLInputElement;
    box.checked = true;
    box.dispatchEvent(new Event("change", { bubbles: true }));
    expect(lines().at(-1)).toBe("1 drawing and 5 alerts (2 on indicators), and their alert log");
  });
});
