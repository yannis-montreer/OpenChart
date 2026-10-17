/** Choosing the code editor: CodeMirror when its chunk loads, a plain text box when it does not. */

export interface Picked<T> {
  editor: T;
  kind: "codemirror" | "textarea";
}

/**
 * `load` builds the code editor (its chunk is fetched the first time this runs, never at page
 * start). If that fails, for any reason (offline, a blocked file), `fallback` builds a plain text
 * box with the same Save and Run, and `onFallback` is told why.
 */
export async function pickEditor<T>(load: () => Promise<T>, fallback: () => T, onFallback?: (why: string) => void): Promise<Picked<T>> {
  try {
    return { editor: await load(), kind: "codemirror" };
  } catch (err) {
    onFallback?.(err instanceof Error ? err.message : String(err));
    return { editor: fallback(), kind: "textarea" };
  }
}

/** The offset where a 1-based line starts; a line past the end is the last one. */
export function lineStart(text: string, line: number): number {
  let at = 0;
  for (let n = 1; n < line; n++) {
    const next = text.indexOf("\n", at);
    if (next < 0) return at;
    at = next + 1;
  }
  return at;
}
