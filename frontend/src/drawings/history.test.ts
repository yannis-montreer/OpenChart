import { describe, expect, it } from "vitest";
import type { Alert, Drawing } from "../api";
import { UndoStack, inverse, renameId, type Entry } from "./history";

const drawing = (id: string, over: Partial<Drawing> = {}): Drawing => ({
  id,
  instrument: "XAU_USD",
  type: "rect",
  points: [
    { t: 1, p: 1 },
    { t: 2, p: 2 },
  ],
  style: { color: "#4d8dff", width: 2, dash: "solid" },
  label: null,
  created_at: 0,
  updated_at: 0,
  ...over,
});

const move = (id: string, a: number, b: number): Entry => ({
  kind: "update",
  id,
  undo: { points: [{ t: a, p: a }] },
  redo: { points: [{ t: b, p: b }] },
});

describe("inverse", () => {
  it("swaps a create and a delete", () => {
    const d = drawing("a");
    expect(inverse({ kind: "create", drawing: d })).toEqual({ kind: "delete", drawing: d });
    expect(inverse({ kind: "delete", drawing: d })).toEqual({ kind: "create", drawing: d });
  });

  it("swaps the two patches of an update", () => {
    expect(inverse(move("a", 1, 2))).toEqual(move("a", 2, 1));
  });

  it("undoes a batch in the opposite order", () => {
    const batch: Entry = { kind: "batch", entries: [move("a", 1, 2), move("b", 3, 4)] };
    expect(inverse(batch)).toEqual({ kind: "batch", entries: [move("b", 4, 3), move("a", 2, 1)] });
  });

  it("is its own inverse twice", () => {
    const e: Entry = { kind: "batch", entries: [move("a", 1, 2), { kind: "delete", drawing: drawing("b") }] };
    expect(inverse(inverse(e))).toEqual(e);
  });
});

describe("renameId", () => {
  it("follows a drawing that was created again under a new id", () => {
    expect(renameId(move("a", 1, 2), "a", "z")).toEqual(move("z", 1, 2));
    expect(renameId(move("b", 1, 2), "a", "z")).toEqual(move("b", 1, 2));
    expect(renameId({ kind: "delete", drawing: drawing("a") }, "a", "z")).toEqual({ kind: "delete", drawing: drawing("z") });
    expect(renameId({ kind: "delete", drawing: drawing("b") }, "a", "z")).toEqual({ kind: "delete", drawing: drawing("b") });
    expect(renameId({ kind: "create", drawing: drawing("b") }, "a", "z")).toEqual({ kind: "create", drawing: drawing("b") });
    expect(renameId({ kind: "create", drawing: drawing("a") }, "a", "z")).toEqual({ kind: "create", drawing: drawing("z") });
    expect(renameId({ kind: "batch", entries: [move("a", 1, 2)] }, "a", "z")).toEqual({ kind: "batch", entries: [move("z", 1, 2)] });
  });
});

describe("UndoStack", () => {
  it("starts empty", () => {
    const s = new UndoStack();
    expect(s.canUndo).toBe(false);
    expect(s.canRedo).toBe(false);
    expect(s.undo()).toBeUndefined();
    expect(s.redo()).toBeUndefined();
  });

  it("undoes the newest first, and redoes in the same order", () => {
    const s = new UndoStack();
    s.push(move("a", 1, 2));
    s.push(move("a", 2, 3));
    expect(s.undo()).toEqual(move("a", 3, 2));
    expect(s.undo()).toEqual(move("a", 2, 1));
    expect(s.canUndo).toBe(false);
    expect(s.redo()).toEqual(move("a", 1, 2));
    expect(s.redo()).toEqual(move("a", 2, 3));
    expect(s.canRedo).toBe(false);
    expect(s.canUndo).toBe(true);
  });

  it("drops the redo list when something new is done", () => {
    const s = new UndoStack();
    s.push(move("a", 1, 2));
    s.undo();
    expect(s.canRedo).toBe(true);
    s.push(move("a", 1, 5));
    expect(s.canRedo).toBe(false);
    expect(s.undo()).toEqual(move("a", 5, 1));
    expect(s.undo()).toBeUndefined();
  });

  it("keeps only the newest `limit` entries", () => {
    const s = new UndoStack(3);
    for (let i = 0; i < 5; i++) s.push(move("a", i, i + 1));
    expect(s.size).toBe(3);
    expect(s.undo()).toEqual(move("a", 5, 4));
    expect(s.undo()).toEqual(move("a", 4, 3));
    expect(s.undo()).toEqual(move("a", 3, 2));
    expect(s.undo()).toBeUndefined();
  });

  it("clears both lists", () => {
    const s = new UndoStack();
    s.push(move("a", 1, 2));
    s.push(move("a", 2, 3));
    s.undo();
    s.clear();
    expect(s.canUndo || s.canRedo).toBe(false);
    expect(s.size).toBe(0);
  });

  it("renames a drawing in the undo and the redo list", () => {
    const s = new UndoStack();
    s.push(move("a", 1, 2));
    s.push(move("a", 2, 3));
    s.undo();
    s.rename("a", "z");
    expect(s.redo()).toEqual(move("z", 2, 3));
    expect(s.undo()).toEqual(move("z", 3, 2));
    expect(s.undo()).toEqual(move("z", 2, 1));
  });
});

const alert = (id: string, drawingId: string): Alert => ({
  id,
  instrument: "XAU_USD",
  type: "line_cross",
  drawing_id: drawingId,
  level: null,
  direction: "cross",
  trigger_price: "mid",
  trigger_mode: "touch",
  timeframe: "1m",
  frequency: "once",
  cooldown_s: 0,
  name: "n",
  expires_at: null,
  notify: ["app"],
  status: "active",
  created_at: 0,
  updated_at: 0,
  last_fired_at: null,
  fire_count: 0,
});

describe("alerts in a delete", () => {
  it("travel with the drawing through the inverse, both ways", () => {
    const alerts = [alert("x", "a")];
    const del: Entry = { kind: "delete", drawing: drawing("a"), alerts };
    expect(inverse(del)).toEqual({ kind: "create", drawing: drawing("a"), alerts });
    expect(inverse(inverse(del))).toEqual(del);
  });

  it("follow a rename of the drawing", () => {
    const alerts = [alert("x", "a")];
    const del: Entry = { kind: "delete", drawing: drawing("a"), alerts };
    expect(renameId(del, "a", "z")).toEqual({ kind: "delete", drawing: drawing("z"), alerts });
  });

  it("are replaced by what the drawing has now, in both lists", () => {
    const s = new UndoStack();
    s.push({ kind: "delete", drawing: drawing("a"), alerts: [alert("x", "a")] });
    s.push(move("b", 1, 2));
    s.undo(); // the move
    s.undo(); // the delete: now a create is next to redo
    s.setAlerts("a", [alert("y", "a"), alert("w", "a")]);
    expect(s.redo()).toEqual({ kind: "delete", drawing: drawing("a"), alerts: [alert("y", "a"), alert("w", "a")] });
  });

  it("are replaced inside a batch too", () => {
    const s = new UndoStack();
    s.push({ kind: "batch", entries: [{ kind: "delete", drawing: drawing("a") }, { kind: "delete", drawing: drawing("b") }] });
    s.setAlerts("a", [alert("y", "a")]);
    expect(s.undo()).toEqual({
      kind: "batch",
      entries: [
        { kind: "create", drawing: drawing("b"), alerts: undefined },
        { kind: "create", drawing: drawing("a"), alerts: [alert("y", "a")] },
      ],
    });
  });

  it("leave other drawings' entries alone", () => {
    const s = new UndoStack();
    s.push({ kind: "delete", drawing: drawing("b"), alerts: [alert("x", "b")] });
    s.setAlerts("a", []);
    expect(s.undo()).toEqual({ kind: "create", drawing: drawing("b"), alerts: [alert("x", "b")] });
  });
});
