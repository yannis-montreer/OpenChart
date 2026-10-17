/**
 * Unsaved drafts: a convenience, never the source of truth. What you typed and did not save
 * is kept in this browser's storage per file, with the version of the stored file it started
 * from. The editor always opens on the server's file; a draft is offered ("Restore draft" or
 * "Discard") only while that file is still the one the draft started from, so a draft can
 * never overwrite a file that changed elsewhere. Storage that is missing, blocked or full is
 * ignored: the editor works without it.
 */

import { isId, sourceProblem } from "./rules";

export type DraftStore = Pick<Storage, "getItem" | "setItem" | "removeItem">;

export interface Draft {
  /** The `version` of the stored file the draft started from. */
  base: string;
  source: string;
  at: number;
}

export interface Stored {
  version: string;
  source: string;
}

const PREFIX = "openchart.draft.v1:";

export function browserDraftStore(): DraftStore | null {
  try {
    return window.localStorage;
  } catch {
    return null; // storage blocked
  }
}

export function dropDraft(store: DraftStore | null, id: string): void {
  if (!isId(id)) return;
  try {
    store?.removeItem(PREFIX + id);
  } catch {
    /* storage unavailable */
  }
}

/** After every edit: keep the buffer as a draft, or drop the draft when the buffer is the stored file. */
export function syncDraft(store: DraftStore | null, id: string, buffer: string, stored: Stored, now = Date.now()): void {
  if (!store || !isId(id)) return;
  if (buffer === stored.source || sourceProblem(buffer) !== null) {
    dropDraft(store, id);
    return;
  }
  try {
    const draft: Draft = { base: stored.version, source: buffer, at: now };
    store.setItem(PREFIX + id, JSON.stringify(draft));
  } catch {
    /* full or blocked: no draft, nothing else changes */
  }
}

/** The draft to offer for the file as the server holds it now, or null (and a stale or damaged one is removed). */
export function readDraft(store: DraftStore | null, id: string, stored: Stored): Draft | null {
  if (!store || !isId(id)) return null;
  let raw: string | null;
  try {
    raw = store.getItem(PREFIX + id);
  } catch {
    return null;
  }
  if (raw === null) return null;
  const draft = parse(raw);
  if (!draft || draft.base !== stored.version || draft.source === stored.source || sourceProblem(draft.source) !== null) {
    dropDraft(store, id);
    return null;
  }
  return draft;
}

function parse(raw: string): Draft | null {
  try {
    const v = JSON.parse(raw) as unknown;
    if (v === null || typeof v !== "object" || Array.isArray(v)) return null;
    const { base, source, at } = v as Record<string, unknown>;
    if (typeof base !== "string" || typeof source !== "string" || typeof at !== "number") return null;
    return { base, source, at };
  } catch {
    return null;
  }
}
