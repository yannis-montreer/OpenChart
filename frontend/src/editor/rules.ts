/**
 * What the editor checks before it asks the server anything (docs/api.md, "Your indicator
 * definitions"; docs/ci4-frontend-plan.md): the same rules the server enforces, so a
 * refusal is shown at once and no request is made for something that cannot work. The
 * server stays the authority: nothing here makes a request safe by itself.
 */

/** The server's cap on a source, in UTF-8 bytes. */
export const MAX_SOURCE_BYTES = 256 * 1024;

const ID = /^[a-z][a-z0-9_]{0,39}$/; // no `m` flag: `$` is the end of the string, never before a final newline

/** A definition id: the file name without `.py`, lowercase, at most 40 characters. */
export function isId(id: string): boolean {
  return ID.test(id);
}

export function idProblem(id: string): string | null {
  if (id === "") return "Give it a name.";
  if (id.length > 40) return "At most 40 characters.";
  if (!/^[a-z]/.test(id)) return "Start with a lowercase letter.";
  if (!isId(id)) return "Use only lowercase letters, digits and _.";
  return null;
}

/** What is typed in an id field: trimmed and lowercased, nothing else changed (the rest is refused, not guessed). */
export function normalizeIdInput(text: string): string {
  return text.trim().toLowerCase();
}

/** The id the server makes of an imported file's name (docs/api.md, Import), for showing it before the request. */
export function idFromFilename(filename: string): string {
  const last = filename.split(/[\\/]/).pop() ?? "";
  const stem = last.replace(/\.py$/i, "");
  if (stem === "") return "imported";
  let id = "";
  for (const ch of stem.toLowerCase()) id += /[a-z0-9_]/.test(ch) ? ch : "_";
  if (/^[0-9_]/.test(id)) id = `x${id}`;
  return id.slice(0, 40);
}

export function sourceBytes(text: string): number {
  return new TextEncoder().encode(text).length;
}

const LONE_SURROGATE = /[\ud800-\udbff](?![\udc00-\udfff])|(?<![\ud800-\udbff])[\udc00-\udfff]/;

/** Why this text cannot be saved or run, or null. */
export function sourceProblem(text: string): string | null {
  if (text.includes("\u0000")) return "The code holds a NUL character.";
  // A surrogate with no partner is not Unicode text; the server refuses it too.
  if (LONE_SURROGATE.test(text)) return "The code is not valid Unicode text.";
  const bytes = sourceBytes(text);
  if (bytes > MAX_SOURCE_BYTES) return `The code is over 256 KB (${Math.ceil(bytes / 1024)} KB).`;
  return null;
}

export function titleProblem(title: string): string | null {
  if (/[\r\n]/.test(title)) return "A title is one line.";
  if (title.trim() === "") return "Give it a title.";
  if ([...title].length > 60) return "At most 60 characters.";
  return null;
}

/** A file picked for Import, checked before it is read into the page. */
export function fileProblem(file: { name: string; size: number }): string | null {
  if (!/\.py$/i.test(file.name)) return "Pick a .py file.";
  if (file.size > MAX_SOURCE_BYTES) return `That file is over 256 KB (${Math.ceil(file.size / 1024)} KB).`;
  return null;
}
