/**
 * Undo and redo of drawing changes made in this browser session (kept in memory only; the
 * server stays the source of truth, so an undo is an ordinary API call made by the
 * controller). Pure: no DOM, no network.
 */

import type { Drawing, DrawingPatch } from "../api";

export type Entry =
  | { kind: "create"; drawing: Drawing }
  | { kind: "delete"; drawing: Drawing }
  | { kind: "update"; id: string; undo: DrawingPatch; redo: DrawingPatch }
  | { kind: "batch"; entries: Entry[] };

/** The entry that takes back `e`. */
export function inverse(e: Entry): Entry {
  switch (e.kind) {
    case "create":
      return { kind: "delete", drawing: e.drawing };
    case "delete":
      return { kind: "create", drawing: e.drawing };
    case "update":
      return { kind: "update", id: e.id, undo: e.redo, redo: e.undo };
    case "batch":
      return { kind: "batch", entries: e.entries.map(inverse).reverse() };
  }
}

/** `e` with drawing `from` called `to` (a deleted drawing comes back under a new id). */
export function renameId(e: Entry, from: string, to: string): Entry {
  switch (e.kind) {
    case "create":
    case "delete":
      return e.drawing.id === from ? { ...e, drawing: { ...e.drawing, id: to } } : e;
    case "update":
      return e.id === from ? { ...e, id: to } : e;
    case "batch":
      return { kind: "batch", entries: e.entries.map((x) => renameId(x, from, to)) };
  }
}

export class UndoStack {
  private done: Entry[] = [];
  private undone: Entry[] = [];

  constructor(private readonly limit = 100) {}

  get canUndo(): boolean {
    return this.done.length > 0;
  }

  get canRedo(): boolean {
    return this.undone.length > 0;
  }

  get size(): number {
    return this.done.length;
  }

  /** Record something just done. What was undone can no longer be redone. */
  push(e: Entry): void {
    this.done.push(e);
    if (this.done.length > this.limit) this.done.shift();
    this.undone = [];
  }

  /** The entry to carry out to take back the newest one. */
  undo(): Entry | undefined {
    const e = this.done.pop();
    if (!e) return undefined;
    this.undone.push(e);
    return inverse(e);
  }

  /** The entry to carry out again. */
  redo(): Entry | undefined {
    const e = this.undone.pop();
    if (!e) return undefined;
    this.done.push(e);
    return e;
  }

  clear(): void {
    this.done = [];
    this.undone = [];
  }

  rename(from: string, to: string): void {
    this.done = this.done.map((e) => renameId(e, from, to));
    this.undone = this.undone.map((e) => renameId(e, from, to));
  }
}
