/**
 * A server for tests and for looking at the editor without a backend: the CI-4 contract of
 * docs/api.md ("Your indicator definitions") as an in-memory `fetch`. Written from the
 * contract's text, not from the client's code, so a client that misreads the contract fails
 * against it. It does not run code: a source "fails to load" when it contains `SYNTAX`, and
 * a run "raises" when it contains `RAISE` (so a test can make either happen).
 *
 * Self-contained on purpose (type imports only): a plain Node script can import it.
 */

import type { IndicatorDef } from "../api";

const CAP = 256 * 1024;
const ID = /^[a-z][a-z0-9_]{0,39}$/;
const TITLE_LINE = /^TITLE = "(.*)"$/gm;

export interface MockOptions {
  /** Editing allowed for this request (default true). */
  allowed?: boolean;
  reason?: string;
  /** The worker runs (default true): a preview answers 503 without it. */
  worker?: boolean;
  /** Seconds the clock moves on at each write (default 1; 0 puts two saves in one second). */
  clockStep?: number;
}

export interface MockCall {
  method: string;
  path: string;
  body: unknown;
}

interface Version {
  version: string;
  saved_at: number;
  source: string;
}

const TEMPLATES: Record<string, { title: string; description: string; source: string }> = {
  blank: { title: "Blank", description: "The smallest file that loads", source: 'TITLE = "Blank"\n' },
  sma_cross: { title: "SMA cross", description: "Two moving averages and their crossings", source: 'TITLE = "SMA cross"\n# fast, slow = ...\n' },
  rsi_bands: { title: "RSI with bands", description: "RSI with two levels", source: 'TITLE = "RSI with bands"\n' },
  bollinger_fill: { title: "Bollinger fill", description: "Bands with a fill", source: 'TITLE = "Bollinger fill"\n' },
  engulfing: { title: "Engulfing", description: "Engulfing candles as markers", source: 'TITLE = "Engulfing"\n' },
};

/** A fixed-size digest, 12 hex digits (the real one is SHA-256; tests only need it to differ with the text). */
export function mockVersion(text: string): string {
  let h1 = 0xdeadbeef;
  let h2 = 0x41c6ce57;
  for (let i = 0; i < text.length; i++) {
    const c = text.charCodeAt(i);
    h1 = Math.imul(h1 ^ c, 2654435761);
    h2 = Math.imul(h2 ^ c, 1597334677);
  }
  h1 = Math.imul(h1 ^ (h1 >>> 16), 2246822507) ^ Math.imul(h2 ^ (h2 >>> 13), 3266489909);
  h2 = Math.imul(h2 ^ (h2 >>> 16), 2246822507) ^ Math.imul(h1 ^ (h1 >>> 13), 3266489909);
  return ((h2 >>> 0).toString(16).padStart(8, "0") + (h1 >>> 0).toString(16).padStart(8, "0")).slice(0, 12);
}

function utf8(text: string): number {
  return new TextEncoder().encode(text).length;
}

function stamp(seconds: number): string {
  return new Date(seconds * 1000).toISOString().replace(/[-:]/g, "").replace(/\.\d+Z$/, "Z");
}

function lineOf(source: string, needle: string): number {
  const at = source.indexOf(needle);
  return at < 0 ? 1 : source.slice(0, at).split("\n").length;
}

function titleOf(source: string, fallback: string): string {
  const found = [...source.matchAll(TITLE_LINE)];
  return found.length === 1 ? found[0][1] : fallback;
}

class Reply extends Error {
  constructor(
    public status: number,
    public body: unknown,
  ) {
    super(String(status));
  }
}

export class MockServer {
  files = new Map<string, string>();
  history = new Map<string, Version[]>();
  /** What was broadcast (`indicators_changed`), in order. */
  broadcasts: { id: string; op: string }[] = [];
  calls: MockCall[] = [];
  allowed: boolean;
  reason: string;
  worker: boolean;
  clock = Date.parse("2026-10-17T08:00:00Z") / 1000;
  private step: number;

  constructor(opts: MockOptions = {}) {
    this.allowed = opts.allowed ?? true;
    this.reason = opts.reason ?? "turned off on the server (openchart settings set indicator-editing on|auto|off)";
    this.worker = opts.worker ?? true;
    this.step = opts.clockStep ?? 1;
  }

  /** Put a file there as a text editor would (no history, no broadcast). */
  place(id: string, source: string): void {
    this.files.set(id, source);
  }

  /** The file as stored, or undefined. */
  read(id: string): string | undefined {
    return this.files.get(id);
  }

  writes(): MockCall[] {
    return this.calls.filter((c) => c.method !== "GET");
  }

  fetch = async (input: string | URL | Request, init: RequestInit = {}): Promise<Response> => {
    const url = new URL(typeof input === "string" || input instanceof URL ? input : input.url, "http://mock/");
    const method = (init.method ?? "GET").toUpperCase();
    let body: unknown;
    if (typeof init.body === "string") {
      try {
        body = JSON.parse(init.body);
      } catch {
        return this.json(400, { detail: "malformed body" });
      }
    }
    this.calls.push({ method, path: url.pathname + url.search, body });
    try {
      return this.route(method, url, body);
    } catch (err) {
      if (err instanceof Reply) return this.json(err.status, err.body);
      throw err;
    }
  };

  private json(status: number, body: unknown): Response {
    return new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
  }

  private fail(status: number, detail: unknown): never {
    throw new Reply(status, { detail });
  }

  private route(method: string, url: URL, body: unknown): Response {
    const base = "/api/custom-indicators";
    const path = url.pathname;
    if (path !== base && !path.startsWith(`${base}/`)) this.fail(404, "Not Found");
    if (!this.allowed) this.fail(403, `Editing custom indicators is off: ${this.reason}.`);
    const parts = path.slice(base.length).split("/").filter(Boolean).map(decodeURIComponent);
    const [first, second, third, fourth] = parts;
    if (parts.length === 0) {
      if (method !== "POST") this.fail(405, "Method Not Allowed");
      return this.create(body);
    }
    if (first === "templates" && parts.length === 1 && method === "GET") {
      return this.json(
        200,
        Object.entries(TEMPLATES).map(([id, t]) => ({ id, title: t.title, description: t.description })),
      );
    }
    if (first === "preview" && parts.length === 1 && method === "POST") return this.preview(body);
    // Everything else is about one file: its id is a path segment, and a bad one is a 404.
    const id = first;
    if (!ID.test(id)) this.fail(404, "No such file.");
    if (parts.length === 1) {
      if (method === "PATCH") return this.rename(id, body);
      if (method === "DELETE") this.fail(405, "not in this mock");
    }
    if (second === "source" && parts.length === 2) {
      if (method === "GET") return this.source(id, url.searchParams.get("download") === "true");
      if (method === "PUT") return this.save(id, body, "save");
    }
    if (second === "duplicate" && parts.length === 2 && method === "POST") return this.duplicate(id, body);
    if (second === "versions" && parts.length === 2 && method === "GET") return this.versions(id);
    if (second === "versions" && parts.length === 3 && method === "GET") return this.version(id, third);
    if (second === "versions" && parts.length === 4 && fourth === "restore" && method === "POST") return this.restore(id, third, body);
    this.fail(404, "Not Found");
  }

  private obj(body: unknown): Record<string, unknown> {
    if (body === null || typeof body !== "object" || Array.isArray(body)) this.fail(400, "The body must be an object.");
    return body as Record<string, unknown>;
  }

  private checkSource(source: unknown): string {
    if (typeof source !== "string") this.fail(400, "source must be text.");
    if (source.includes("\u0000")) this.fail(400, "The source holds a NUL character.");
    if (/[\ud800-\udbff](?![\udc00-\udfff])|(?<![\ud800-\udbff])[\udc00-\udfff]/.test(source)) this.fail(400, "The source is not valid Unicode.");
    if (utf8(source) > CAP) this.fail(413, "The source is over 256 KB.");
    return source.startsWith("﻿") ? source.slice(1) : source;
  }

  private free(id: string): string {
    for (let n = 2; ; n++) if (!this.files.has(`${id}_${n}`) && `${id}_${n}`.length <= 40) return `${id}_${n}`;
  }

  private exists(id: string): never {
    this.fail(409, { message: `A file named ${id}.py already exists.`, suggested: this.free(id) });
  }

  private tick(): void {
    this.clock += this.step;
  }

  private definition(id: string, source: string, op: string, unchanged: boolean): Response {
    const bad = source.includes("SYNTAX");
    const title = titleOf(source, id);
    const line = bad ? lineOf(source, "SYNTAX") : null;
    const def: IndicatorDef | null = bad ? null : this.def(id, title);
    return this.json(op === "create" || op === "duplicate" ? 201 : 200, {
      id,
      kind: "custom",
      file: `${id}.py`,
      status: bad ? "error" : "ok",
      reason: bad ? "SyntaxError: invalid syntax" : null,
      line,
      definition: def,
      checks: [],
      version: mockVersion(source),
      saved: true,
      unchanged,
      error: bad ? { type: "SyntaxError", message: "invalid syntax", line, traceback: `  line ${line}\n    SYNTAX` } : null,
    });
  }

  private def(id: string, title: string): IndicatorDef {
    return {
      id,
      name: title,
      title,
      overlay: true,
      builtin: false,
      kind: "custom",
      params: [
        { name: "color", type: "color", default: "#ff9800", label: "Colour", group: "style" },
        { name: "width", type: "width", default: 1, label: "Width", group: "style" },
      ],
      outputs: [{ name: "v", label: "V", color: "color", width: "width" }],
    };
  }

  private broadcast(id: string, op: string): void {
    this.broadcasts.push({ id, op });
  }

  private create(body: unknown): Response {
    const b = this.obj(body);
    if (typeof b.template === "string") {
      const id = b.id;
      if (typeof id !== "string" || !ID.test(id)) this.fail(400, "A file name is lowercase letters, digits and _.");
      const template = TEMPLATES[b.template];
      if (!template) this.fail(400, "No such template.");
      if (this.files.has(id)) this.exists(id);
      let title = template.title;
      if (b.title !== undefined) {
        if (typeof b.title !== "string" || [...b.title].length < 1 || [...b.title].length > 60 || /[\r\n]/.test(b.title)) this.fail(400, "A title is 1 to 60 characters on one line.");
        title = b.title;
      }
      const source = template.source.replace(TITLE_LINE, () => `TITLE = "${title}"`);
      this.tick();
      this.files.set(id, source);
      this.broadcast(id, "create");
      return this.definition(id, source, "create", false);
    }
    if ("source" in b) {
      const source = this.checkSource(b.source);
      let id = b.id;
      if (id === undefined) {
        const stem = typeof b.filename === "string" ? (b.filename.split(/[\\/]/).pop() ?? "").replace(/\.py$/i, "") : "";
        let made = "";
        for (const ch of stem.toLowerCase()) made += /[a-z0-9_]/.test(ch) ? ch : "_";
        if (made === "") made = "imported";
        if (/^[0-9_]/.test(made)) made = `x${made}`;
        id = made.slice(0, 40);
      }
      if (typeof id !== "string" || !ID.test(id)) this.fail(400, "A file name is lowercase letters, digits and _.");
      if (this.files.has(id)) this.exists(id);
      this.tick();
      this.files.set(id, source);
      this.broadcast(id, "create");
      return this.definition(id, source, "create", false);
    }
    this.fail(400, "Send a template or a source.");
  }

  private source(id: string, download: boolean): Response {
    const source = this.need(id);
    if (download) {
      return new Response(source, {
        status: 200,
        headers: { "Content-Type": "text/x-python; charset=utf-8", "Content-Disposition": `attachment; filename="${id}.py"` },
      });
    }
    return this.json(200, { id, file: `${id}.py`, version: mockVersion(source), source, bytes: utf8(source), updated_at: this.clock });
  }

  private need(id: string): string {
    const source = this.files.get(id);
    if (source === undefined) this.fail(404, `No file of yours named ${id}.py.`);
    return source;
  }

  private save(id: string, body: unknown, op: string): Response {
    const b = this.obj(body);
    const current = this.need(id);
    const source = this.checkSource(b.source);
    if (b.base_version !== undefined && b.base_version !== mockVersion(current)) {
      this.fail(409, { message: "The file changed since you opened it.", version: mockVersion(current) });
    }
    return this.write(id, current, source, op);
  }

  private write(id: string, current: string, source: string, op: string): Response {
    if (source === current) return this.definition(id, source, op, true);
    this.tick();
    const list = this.history.get(id) ?? [];
    let version = stamp(this.clock);
    for (let n = 2; list.some((v) => v.version === version); n++) version = `${stamp(this.clock)}-${n}`;
    list.unshift({ version, saved_at: this.clock, source: current });
    this.history.set(id, list.slice(0, 10));
    this.files.set(id, source);
    this.broadcast(id, op);
    return this.definition(id, source, op, false);
  }

  private rename(id: string, body: unknown): Response {
    const b = this.obj(body);
    const current = this.need(id);
    const title = b.title;
    if (typeof title !== "string" || [...title].length < 1 || [...title].length > 60 || /[\r\n]/.test(title)) this.fail(400, "A title is 1 to 60 characters on one line.");
    if ([...current.matchAll(TITLE_LINE)].length !== 1) this.fail(409, { message: "edit TITLE in the code" });
    return this.write(id, current, current.replace(TITLE_LINE, () => `TITLE = "${title}"`), "rename");
  }

  private duplicate(id: string, body: unknown): Response {
    const b = this.obj(body ?? {});
    const current = this.need(id);
    const to = b.id === undefined ? this.free(id) : b.id;
    if (typeof to !== "string" || !ID.test(to)) this.fail(400, "A file name is lowercase letters, digits and _.");
    if (this.files.has(to)) this.exists(to);
    let source = current;
    if ([...current.matchAll(TITLE_LINE)].length === 1) {
      const title = typeof b.title === "string" ? b.title : `${titleOf(current, id)} (copy)`;
      source = current.replace(TITLE_LINE, () => `TITLE = "${title}"`);
    }
    this.tick();
    this.files.set(to, source);
    this.broadcast(to, "duplicate");
    return this.definition(to, source, "duplicate", false);
  }

  private versions(id: string): Response {
    this.need(id);
    return this.json(
      200,
      (this.history.get(id) ?? []).map((v) => ({ version: v.version, saved_at: v.saved_at, bytes: utf8(v.source), digest: mockVersion(v.source) })),
    );
  }

  private version(id: string, version: string): Response {
    this.need(id);
    const v = (this.history.get(id) ?? []).find((x) => x.version === version);
    if (!v) this.fail(404, "No such version.");
    return this.json(200, { id, version: v.version, source: v.source, saved_at: v.saved_at });
  }

  private restore(id: string, version: string, body: unknown): Response {
    const b = this.obj(body ?? {});
    const current = this.need(id);
    const v = (this.history.get(id) ?? []).find((x) => x.version === version);
    if (!v) this.fail(404, "No such version.");
    if (b.base_version !== undefined && b.base_version !== mockVersion(current)) {
      this.fail(409, { message: "The file changed since you opened it.", version: mockVersion(current) });
    }
    return this.write(id, current, v.source, "restore");
  }

  private preview(body: unknown): Response {
    const b = this.obj(body);
    const source = this.checkSource(b.source);
    if (typeof b.instrument !== "string" || typeof b.timeframe !== "string") this.fail(400, "instrument and timeframe are needed.");
    if (!this.worker) this.fail(503, "The indicator worker is not running.");
    const file = typeof b.id === "string" && ID.test(b.id) ? `${b.id}.py` : "preview.py";
    for (const [needle, type, message] of [
      ["SYNTAX", "SyntaxError", "invalid syntax"],
      ["RAISE", "ZeroDivisionError", "division by zero"],
    ] as const) {
      if (source.includes(needle)) {
        const line = lineOf(source, needle);
        this.fail(422, { type, message, line, traceback: `  File "${file}", line ${line}, in compute\n    ${needle}` });
      }
    }
    const id = typeof b.id === "string" && ID.test(b.id) ? b.id : "preview";
    return this.json(200, {
      id,
      params: b.params ?? {},
      t: [1000, 2000, 3000],
      values: { v: [1, 2, 3] },
      definition: this.def(id, titleOf(source, id)),
      preview: true,
    });
  }
}
