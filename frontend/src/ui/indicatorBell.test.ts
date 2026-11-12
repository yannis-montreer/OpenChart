// @vitest-environment happy-dom
import { describe, expect, it, vi } from "vitest";

import type { Alert } from "../api";
import { bellButton } from "./indicatorBell";

const a = (status: Alert["status"]) => ({ status }) as Alert;

describe("the bell on an indicator's legend row", () => {
  it("is plain with no alerts, and asks to add one", () => {
    const b = bellButton([], vi.fn());
    expect(b.getAttribute("aria-label")).toBe("Add alert on this indicator");
    expect(b.querySelector(".bell-count")).toBeNull();
    expect(b.classList.contains("alert-on")).toBe(false);
  });

  it("shows how many alerts the instance has, lit while one is active", () => {
    const b = bellButton([a("active"), a("stopped")], vi.fn());
    expect(b.querySelector(".bell-count")?.textContent).toBe("2");
    expect(b.classList.contains("alert-on")).toBe(true);
    expect(b.getAttribute("aria-label")).toBe("2 alerts on this indicator (1 active)");
  });

  it("is dimmed when none is active, but still counts", () => {
    const b = bellButton([a("stopped")], vi.fn());
    expect(b.classList.contains("alert-off")).toBe(true);
    expect(b.querySelector(".bell-count")?.textContent).toBe("1");
  });

  it("opens the dialog on click", () => {
    const open = vi.fn();
    bellButton([], open).click();
    expect(open).toHaveBeenCalledTimes(1);
  });
});
