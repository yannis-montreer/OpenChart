/**
 * The text box of the editor panel. `openCodeEditor` loads CodeMirror (its own chunk, fetched
 * the first time the editor opens) and falls back to a plain `<textarea>` with the same Save and
 * Run keys if that does not work, so a failed download never costs anyone their file.
 */

import { h } from "../ui/dom";
import { lineStart, pickEditor } from "./pickEditor";

export interface CodeEditor {
  text(): string;
  /** Replace everything (not reported as typing). */
  setText(text: string): void;
  focus(): void;
  /** Move the cursor to the start of a 1-based line and bring it into view. */
  goToLine(line: number): void;
  /** Tint a line (or none). */
  markError(line: number | null): void;
  setReadOnly(readOnly: boolean): void;
  destroy(): void;
}

export interface EditorHooks {
  onInput(text: string): void;
  onSave(): void;
  onRun(): void;
}

function textareaEditor(host: HTMLElement, hooks: EditorHooks, doc: string): CodeEditor {
  const area = h("textarea", { class: "ed-textarea", spellcheck: "false", "aria-label": "Indicator source code", wrap: "off" });
  area.value = doc;
  let quiet = false;
  area.addEventListener("input", () => {
    if (!quiet) hooks.onInput(area.value);
  });
  area.addEventListener("keydown", (ev) => {
    if (!(ev.ctrlKey || ev.metaKey)) return;
    if (ev.key === "s") {
      ev.preventDefault();
      hooks.onSave();
    } else if (ev.key === "Enter") {
      ev.preventDefault();
      hooks.onRun();
    }
  });
  host.append(area);
  return {
    text: () => area.value,
    setText(text) {
      quiet = true;
      area.value = text;
      quiet = false;
    },
    focus: () => area.focus(),
    goToLine(line) {
      const at = lineStart(area.value, line);
      area.focus();
      area.setSelectionRange(at, at);
      const lineHeight = parseFloat(getComputedStyle(area).lineHeight) || 18;
      area.scrollTop = Math.max(0, (Math.max(1, line) - 4) * lineHeight);
    },
    markError: () => undefined, // the error panel names the line; a text box has nothing to tint
    setReadOnly: (ro) => {
      area.readOnly = ro;
    },
    destroy: () => area.remove(),
  };
}

export async function openCodeEditor(
  host: HTMLElement,
  hooks: EditorHooks,
  doc: string,
  onFallback: (why: string) => void,
): Promise<{ editor: CodeEditor; kind: "codemirror" | "textarea" }> {
  return pickEditor(
    async () => {
      const { createCodeMirror } = await import("./codemirror");
      return createCodeMirror(host, hooks, doc);
    },
    () => textareaEditor(host, hooks, doc),
    onFallback,
  );
}
