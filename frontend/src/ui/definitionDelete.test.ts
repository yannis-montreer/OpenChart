import { describe, expect, it } from "vitest";

import { ApiError, type DefinitionUsage } from "../api";
import { afterDelete, afterError, afterUsage, confirmLabel, usageLines } from "./definitionDelete";

const USAGE: DefinitionUsage = {
  id: "sma_cross",
  in_use: true,
  charts: [
    { source: "oanda", instrument: "EUR_USD", instances: 1 },
    { source: "oanda", instrument: "XAU_USD", instances: 2 },
  ],
  layouts: [{ id: "l1", name: "Gold H1", instances: 1 }],
};
const IDLE: DefinitionUsage = { id: "idle", in_use: false, charts: [], layouts: [] };
const MESSAGE = '"SMA cross" is on 2 charts (EUR_USD, XAU_USD) and in 1 saved layout ("Gold H1"). Deleting it removes it from all of them.';

describe("usageLines", () => {
  it("name each chart and layout with how many times it is used", () => {
    expect(usageLines(USAGE)).toEqual({
      charts: ["EUR_USD (1 indicator)", "XAU_USD (2 indicators)"],
      layouts: ["Gold H1 (1 indicator)"],
    });
  });

  it("are empty for a definition that is not used", () => {
    expect(usageLines(IDLE)).toEqual({ charts: [], layouts: [] });
  });
});

describe("afterUsage", () => {
  it("asks a plain question when nothing uses the file, and says where it goes", () => {
    const next = afterUsage("SMA cross", IDLE);
    expect(next).toMatchObject({ phase: "confirm", inUse: false, usage: IDLE });
    expect(next.text).toBe('Delete "SMA cross"? The file moves to the .deleted folder inside your indicators folder; nothing is erased.');
  });

  it("lists where it is used when something does", () => {
    const next = afterUsage("SMA cross", USAGE);
    expect(next).toMatchObject({ phase: "confirm", inUse: true, usage: USAGE });
    expect(next.text).toBe('"SMA cross" is used. Deleting it removes it from all of these; the file moves to the .deleted folder.');
  });
});

describe("confirmLabel", () => {
  it("says what the button will do", () => {
    expect(confirmLabel(false)).toBe("Delete");
    expect(confirmLabel(true)).toBe("Delete and remove from all");
  });
});

describe("afterError", () => {
  it("403: blocked, with the server's reason", () => {
    const next = afterError("SMA cross", new ApiError(403, "turned off on the server (openchart settings set indicator-editing on|auto|off)"));
    expect(next).toEqual({
      phase: "blocked",
      text: "Deleting is not allowed here: turned off on the server (openchart settings set indicator-editing on|auto|off)",
      refresh: false,
    });
  });

  it("404 from a server without the routes: unsupported", () => {
    const next = afterError("SMA cross", new ApiError(404, "Not Found"));
    expect(next).toMatchObject({ phase: "unsupported", refresh: false });
    expect(next.text).toBe("This server cannot delete indicators yet. Update OpenChart on the server first.");
  });

  it("404 with the server's own message: the file is already gone, the list is refreshed", () => {
    const next = afterError("SMA cross", new ApiError(404, "no indicator of yours named 'sma_cross'"));
    expect(next).toEqual({ phase: "gone", text: "no indicator of yours named 'sma_cross'", refresh: true });
  });

  it("409 with a usage: the file became used meanwhile, ask again with the list", () => {
    const err = new ApiError(409, MESSAGE);
    err.detail = { message: MESSAGE, usage: USAGE };
    expect(afterError("SMA cross", err)).toMatchObject({ phase: "confirm", inUse: true, usage: USAGE, text: MESSAGE, refresh: false });
  });

  it("409 without a usage is a failure, not a question", () => {
    const next = afterError("SMA cross", new ApiError(409, "something else"));
    expect(next).toMatchObject({ phase: "failed", refresh: true });
    expect(next.text).toBe("Could not delete: something else");
  });

  it("a server error: failed, and everything shown is re-read (the move may be half done)", () => {
    const next = afterError("SMA cross", new ApiError(500, "Internal Server Error"));
    expect(next).toEqual({
      phase: "failed",
      text: "Could not delete: Internal Server Error. Check the list: the file or the charts may have changed.",
      refresh: true,
    });
  });

  it("no answer at all: failed, re-read", () => {
    const next = afterError("SMA cross", new TypeError("Failed to fetch"));
    expect(next).toMatchObject({ phase: "failed", refresh: true });
    expect(next.text).toContain("Failed to fetch");
  });
});

describe("afterDelete", () => {
  it("says where the file went and what it was removed from", () => {
    const done = afterDelete("SMA cross", { id: "sma_cross", moved_to: ".deleted/sma_cross-20261016T120000Z", removed: USAGE });
    expect(done).toEqual({
      title: "Deleted",
      body: 'Moved "SMA cross" to .deleted/sma_cross-20261016T120000Z. Removed from 2 charts and 1 saved layout.',
    });
  });

  it("does not mention removals when there were none", () => {
    expect(afterDelete("Idle", { id: "idle", moved_to: ".deleted/idle-x", removed: IDLE }).body).toBe('Moved "Idle" to .deleted/idle-x.');
  });

  it("uses the singular", () => {
    const one: DefinitionUsage = { id: "a", in_use: true, charts: [{ source: "s", instrument: "EUR_USD", instances: 3 }], layouts: [] };
    expect(afterDelete("A", { id: "a", moved_to: ".deleted/a-x", removed: one }).body).toBe('Moved "A" to .deleted/a-x. Removed from 1 chart.');
  });
});
