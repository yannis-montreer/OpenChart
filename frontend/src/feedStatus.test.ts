import { describe, expect, it } from "vitest";
import type { RetryStatus } from "./api";
import { retryIndicator, shortDuration } from "./feedStatus";

const ERROR = "OANDA rejected the credentials: Insufficient authorization to perform request.";
const retry = (over: Partial<RetryStatus> = {}): RetryStatus => ({
  reason: "credentials_rejected",
  since: 1000,
  attempts: 3,
  next_attempt: 1500,
  next_attempt_in: 120,
  gives_up_at: 1000 + 6 * 3600,
  ...over,
});

describe("shortDuration", () => {
  it("reads as seconds, minutes, then hours and minutes", () => {
    expect([shortDuration(0), shortDuration(44.6), shortDuration(90), shortDuration(600)]).toEqual([
      "0 s",
      "45 s",
      "2 min",
      "10 min",
    ]);
    expect(shortDuration(6 * 3600)).toBe("6 h 00 min");
    expect(shortDuration(3900)).toBe("1 h 05 min");
  });
});

describe("retryIndicator", () => {
  it("says the feed is down, why, when the next attempt is and when it stops", () => {
    const { label, detail } = retryIndicator(ERROR, retry());
    expect(label).toBe("Feed down, retrying");
    expect(detail).toContain(ERROR + " The token may still be valid"); // one full stop, not two
    expect(detail).toContain("next attempt in 2 min (retry 3)");
    expect(detail).toContain("stops after 6 h 00 min");
  });

  it("says when an attempt is running", () => {
    expect(retryIndicator(ERROR, retry({ next_attempt: null, next_attempt_in: null })).detail).toContain(
      "trying again now (retry 3)",
    );
  });

  it("still says something without the error text", () => {
    expect(retryIndicator(null, retry()).detail.startsWith("OANDA rejected the credentials.")).toBe(true);
  });
});
