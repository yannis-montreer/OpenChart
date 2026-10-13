import { describe, expect, it } from "vitest";
import { lineAlertState, lineAlertTitle, pointAlong } from "./alertState";

describe("the alert state a line shows (SPEC section 10 bell indicator)", () => {
  it("is none without alerts, active while one watches, inactive when all are stopped or done", () => {
    expect(lineAlertState([])).toBeNull();
    expect(lineAlertState([{ status: "active" }])).toBe("active");
    expect(lineAlertState([{ status: "stopped" }, { status: "active" }])).toBe("active");
    expect(lineAlertState([{ status: "stopped" }])).toBe("inactive");
    expect(lineAlertState([{ status: "expired" }])).toBe("inactive");
    expect(lineAlertState([{ status: "triggered" }, { status: "expired" }])).toBe("inactive");
  });

  it("says so in the bell's tooltip", () => {
    expect(lineAlertTitle([])).toBe("Add alert on this line (Alt+A)");
    expect(lineAlertTitle([{ status: "active" }])).toBe("Edit alert on this line (Alt+A)");
    expect(lineAlertTitle([{ status: "expired" }])).toBe("Edit alert on this line (expired) (Alt+A)");
    expect(lineAlertTitle([{ status: "stopped" }, { status: "expired" }])).toBe("Edit 2 alerts on this line (none active) (Alt+A)");
  });

  it("places the marker along the visible line", () => {
    expect(pointAlong([], 0.5)).toBeNull();
    expect(pointAlong([[0, 10], [100, 10]], 0.75)).toEqual([75, 10]);
    // A bent polyline (a line drawn across a closure): measured along its length.
    expect(pointAlong([[0, 0], [30, 40], [30, 90]], 0.5)).toEqual([30, 40]);
    expect(pointAlong([[5, 5], [5, 5]], 0.5)).toEqual([5, 5]);
  });
});
