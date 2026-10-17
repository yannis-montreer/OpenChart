/**
 * The editor's state machine for one file, with no DOM (docs/ci4-frontend-plan.md): what is
 * stored on the server, what is in the buffer, what a save, a run or a change elsewhere does
 * to them. The panel only draws `state` and calls these methods.
 *
 * Rules that hold throughout:
 * - the server's file is the truth; the buffer is an edit of it, and a draft is only a
 *   convenience offered while the stored version is the one it started from;
 * - a save carries the version it started from, so a file changed elsewhere is a conflict the
 *   user settles ("Reload" or "Keep mine"), never an overwrite made by accident;
 * - nothing is requested for a text the server would refuse (over 256 KB, NUL, not Unicode) or
 *   while editing is off;
 * - only the newest Run counts: an older answer, or error, that arrives late is dropped.
 */

import {
  ApiError,
  type PreviewQuery,
  type PreviewValues,
  type SavedDefinition,
  type SourceInfo,
  type VersionInfo,
  type VersionSource,
  type customApi,
} from "../api";
import { dropDraft, readDraft, syncDraft, type Draft, type DraftStore } from "./draft";
import { errorFromApi, errorOf, findingsOf, type ErrorInfo, type Finding } from "./errors";
import { sourceProblem } from "./rules";

export type EditorApi = Pick<typeof customApi, "source" | "save" | "importSource" | "versions" | "version" | "restore" | "preview">;

export interface Gate {
  allowed: boolean;
  reason: string | null;
}

export type NoticeKind = "saved" | "unchanged" | "restored" | "conflict" | "remote" | "gone" | "off" | "failed";

export interface Notice {
  kind: NoticeKind;
  text: string;
}

export type Phase = "loading" | "ready" | "failed" | "gone";

export interface EditorState {
  id: string;
  phase: Phase;
  /** Why the file could not be read (phase `failed`). */
  loadError: string | null;
  /** The file as the server holds it, as last read or saved. */
  stored: { version: string; source: string } | null;
  buffer: string;
  dirty: boolean;
  /** Why the buffer cannot be saved or run (size, NUL, not Unicode), or null. */
  problem: string | null;
  editing: Gate;
  saving: boolean;
  running: boolean;
  notice: Notice | null;
  error: ErrorInfo | null;
  findings: Finding[];
  /** A draft of this file that can be restored. */
  draft: Draft | null;
  /** The stored file changed since the buffer started from it: `version` is the stored one. */
  conflict: { version: string } | null;
  versions: VersionInfo[] | null;
  /** A version opened for reading (the buffer is not touched). */
  viewing: VersionSource | null;
}

export interface SessionOptions {
  id: string;
  api: EditorApi;
  store: DraftStore | null;
  editing: Gate;
  onChange: (state: EditorState) => void;
  /** The preview of a Run to draw (with the text it was made from), or null to remove it. */
  onPreview: (values: PreviewValues | null, source?: string) => void;
}

export type RunQuery = Pick<PreviewQuery, "instrument" | "timeframe" | "price" | "params" | "from" | "to" | "limit">;

function messageOf(err: unknown): string {
  return err instanceof Error ? err.message : String(err);
}

export class EditorSession {
  state: EditorState;
  private runToken = 0;
  private closed = false;

  constructor(private opts: SessionOptions) {
    this.state = {
      id: opts.id,
      phase: "loading",
      loadError: null,
      stored: null,
      buffer: "",
      dirty: false,
      problem: null,
      editing: opts.editing,
      saving: false,
      running: false,
      notice: null,
      error: null,
      findings: [],
      draft: null,
      conflict: null,
      versions: null,
      viewing: null,
    };
  }

  private set(patch: Partial<EditorState>): void {
    this.state = { ...this.state, ...patch };
    this.opts.onChange(this.state);
  }

  private get id(): string {
    return this.opts.id;
  }

  /** The catalog says editing is now allowed or not. */
  setEditing(gate: Gate): void {
    this.set({ editing: gate });
  }

  private turnOff(err: ApiError): void {
    this.set({ editing: { allowed: false, reason: err.message }, notice: { kind: "off", text: err.message } });
  }

  private take(info: SourceInfo, patch: Partial<EditorState> = {}): void {
    const stored = { version: info.version, source: info.source };
    this.set({
      phase: "ready",
      loadError: null,
      stored,
      buffer: info.source,
      dirty: false,
      problem: sourceProblem(info.source),
      draft: readDraft(this.opts.store, this.id, stored),
      conflict: null,
      ...patch,
    });
  }

  // Reading

  async open(): Promise<void> {
    this.set({ phase: "loading", loadError: null });
    try {
      this.take(await this.opts.api.source(this.id));
    } catch (err) {
      if (err instanceof ApiError && err.status === 404) {
        this.set({ phase: "gone" });
      } else if (err instanceof ApiError && err.status === 403) {
        this.set({ phase: "failed", loadError: err.message, editing: { allowed: false, reason: err.message } });
      } else {
        this.set({ phase: "failed", loadError: messageOf(err) });
      }
    }
  }

  /** Take the server's file, dropping what was typed (the answer to a conflict: "Reload"). */
  async reload(): Promise<void> {
    dropDraft(this.opts.store, this.id);
    await this.open();
  }

  /** The file may have changed (a broadcast, or the page came back): read it and settle what differs. */
  async refresh(op: string): Promise<void> {
    if (this.closed || this.state.phase === "loading") return;
    if (op === "delete") {
      this.set({ phase: "gone", notice: { kind: "gone", text: "This file was deleted. Your text is kept: Import it to save it again." } });
      return;
    }
    if (this.state.saving) return; // the save carries its own base version: it settles itself
    let info: SourceInfo;
    try {
      info = await this.opts.api.source(this.id);
    } catch (err) {
      if (err instanceof ApiError && err.status === 404) {
        this.set({ phase: "gone", notice: { kind: "gone", text: "This file is gone. Your text is kept: Import it to save it again." } });
      }
      return;
    }
    if (this.closed || !this.state.stored || info.version === this.state.stored.version) return;
    if (!this.state.dirty) {
      this.take(info, { notice: { kind: "remote", text: "This file was changed elsewhere; the new text is shown." } });
    } else {
      this.set({
        conflict: { version: info.version },
        notice: { kind: "conflict", text: "This file was changed elsewhere. Reload it, or keep your text and save over it." },
      });
    }
  }

  // Editing

  edit(text: string): void {
    const { phase, stored } = this.state;
    if (phase !== "ready" && phase !== "gone") return;
    this.set({ buffer: text, dirty: stored ? text !== stored.source : true, problem: sourceProblem(text) });
    if (stored && phase === "ready") syncDraft(this.opts.store, this.id, text, stored);
  }

  restoreDraft(): void {
    const draft = this.state.draft;
    if (!draft || this.state.phase !== "ready") return;
    this.set({ buffer: draft.source, dirty: true, problem: sourceProblem(draft.source), draft: null });
  }

  discardDraft(): void {
    dropDraft(this.opts.store, this.id);
    this.set({ draft: null });
  }

  /** True while there is text the server does not hold. */
  hasUnsaved(): boolean {
    return this.state.phase === "gone" ? this.state.buffer !== "" : this.state.dirty;
  }

  // Writing

  private blocked(): boolean {
    const { editing, problem } = this.state;
    if (!editing.allowed) {
      this.set({ notice: { kind: "off", text: `Editing is off: ${editing.reason ?? "this server does not allow it"}` } });
      return true;
    }
    if (problem) {
      this.set({ notice: { kind: "failed", text: problem } });
      return true;
    }
    return false;
  }

  private conflicted(err: ApiError): boolean {
    const detail = err.detail as { version?: unknown } | undefined;
    if (err.status !== 409 || typeof detail?.version !== "string") return false;
    this.set({
      conflict: { version: detail.version },
      notice: { kind: "conflict", text: "This file was changed elsewhere. Reload it, or keep your text and save over it." },
    });
    return true;
  }

  private applied(r: SavedDefinition, source: string, notice: Notice): void {
    const stored = { version: r.version, source };
    const dirty = this.state.buffer !== source;
    this.set({ stored, dirty, conflict: null, error: errorOf(r.error), findings: findingsOf(r.checks, r.version), notice, draft: null });
    syncDraft(this.opts.store, this.id, this.state.buffer, stored);
  }

  async save(opts: { keepMine?: boolean } = {}): Promise<void> {
    if (this.state.phase !== "ready" || !this.state.stored || this.state.saving) return;
    if (this.blocked()) return;
    const sent = this.state.buffer;
    this.set({ saving: true });
    try {
      const body = opts.keepMine ? { source: sent } : { source: sent, base_version: this.state.stored.version };
      const r = await this.opts.api.save(this.id, body);
      this.applied(r, sent, r.unchanged ? { kind: "unchanged", text: "No changes to save." } : { kind: "saved", text: "Saved." });
    } catch (err) {
      if (err instanceof ApiError && this.conflicted(err)) {
        /* settled by the user */
      } else if (err instanceof ApiError && err.status === 404) {
        this.set({ phase: "gone", notice: { kind: "gone", text: "This file is gone. Your text is kept: Import it to save it again." } });
      } else if (err instanceof ApiError && err.status === 403) {
        this.turnOff(err);
      } else {
        this.set({ notice: { kind: "failed", text: messageOf(err) } });
      }
    } finally {
      this.set({ saving: false });
    }
  }

  /** The file was deleted while the editor was open: put the buffer back as a new file under the same id (never over one). */
  async importBuffer(): Promise<void> {
    if (this.state.phase !== "gone" || this.state.saving) return;
    if (this.blocked()) return;
    const sent = this.state.buffer;
    this.set({ saving: true });
    try {
      const r = await this.opts.api.importSource({ source: sent, id: this.id });
      this.set({ phase: "ready", stored: { version: r.version, source: sent } });
      this.applied(r, sent, { kind: "saved", text: "Saved as a new file." });
    } catch (err) {
      if (err instanceof ApiError && err.status === 403) this.turnOff(err);
      else this.set({ notice: { kind: "failed", text: messageOf(err) } });
    } finally {
      this.set({ saving: false });
    }
  }

  // Running

  async run(query: RunQuery): Promise<void> {
    if (this.state.phase !== "ready" || this.closed) return;
    if (this.blocked()) return;
    const token = ++this.runToken;
    const sent = this.state.buffer;
    this.set({ running: true });
    try {
      const values = await this.opts.api.preview({ source: sent, id: this.id, ...query });
      if (token !== this.runToken) return;
      this.set({ running: false, error: null });
      this.opts.onPreview(values, sent);
    } catch (err) {
      if (token !== this.runToken) return;
      this.set({ running: false, error: errorFromApi(err) });
      if (err instanceof ApiError && err.status === 403) this.turnOff(err);
      this.opts.onPreview(null);
    }
  }

  /** The editor is closed: a run in flight is dropped and the preview goes. */
  close(): void {
    this.closed = true;
    this.runToken++;
    this.set({ running: false });
    this.opts.onPreview(null);
  }

  // Versions

  async loadVersions(): Promise<void> {
    try {
      this.set({ versions: await this.opts.api.versions(this.id) });
    } catch (err) {
      this.set({ notice: { kind: "failed", text: messageOf(err) } });
    }
  }

  async view(version: string): Promise<void> {
    try {
      this.set({ viewing: await this.opts.api.version(this.id, version) });
    } catch (err) {
      this.set({ viewing: null, notice: { kind: "failed", text: messageOf(err) } });
    }
  }

  closeView(): void {
    this.set({ viewing: null });
  }

  /** A save of that version's text: the current file goes into the history first, so it can be undone. */
  async restoreVersion(version: string): Promise<void> {
    if (this.state.phase !== "ready" || !this.state.stored || this.state.saving) return;
    if (!this.state.editing.allowed) {
      this.blocked();
      return;
    }
    this.set({ saving: true });
    try {
      const viewing = this.state.viewing;
      const text = (viewing?.version === version ? viewing : await this.opts.api.version(this.id, version)).source;
      const r = await this.opts.api.restore(this.id, version, { base_version: this.state.stored.version });
      this.set({ buffer: text, problem: sourceProblem(text), viewing: null });
      this.applied(r, text, { kind: "restored", text: "Restored. The text it replaced is in the versions list." });
      await this.loadVersions();
    } catch (err) {
      if (err instanceof ApiError && this.conflicted(err)) {
        /* settled by the user */
      } else if (err instanceof ApiError && err.status === 403) {
        this.turnOff(err);
      } else {
        this.set({ notice: { kind: "failed", text: messageOf(err) } });
      }
    } finally {
      this.set({ saving: false });
    }
  }
}
