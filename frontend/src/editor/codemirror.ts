/**
 * CodeMirror 6 with its Python mode, behind the small `CodeEditor` interface. This is the only
 * file that imports it, and `codeEditor.ts` imports this one with a dynamic `import()`, so the
 * library is its own chunk, fetched the first time the editor opens and never before.
 *
 * Colours are CSS variables of the page, so a theme switch needs no work here.
 */

import { defaultKeymap, history, historyKeymap, indentWithTab } from "@codemirror/commands";
import { python } from "@codemirror/lang-python";
import { HighlightStyle, bracketMatching, indentOnInput, syntaxHighlighting } from "@codemirror/language";
import { EditorState, StateEffect, StateField, Compartment } from "@codemirror/state";
import { Decoration, EditorView, drawSelection, highlightActiveLine, highlightActiveLineGutter, keymap, lineNumbers } from "@codemirror/view";
import { tags } from "@lezer/highlight";

import type { CodeEditor, EditorHooks } from "./codeEditor";

const markLine = StateEffect.define<number | null>();

/** The line an error names (1-based), tinted. */
const errorLine = StateField.define({
  create: () => Decoration.none,
  update(deco, tr) {
    deco = deco.map(tr.changes);
    for (const e of tr.effects) {
      if (!e.is(markLine)) continue;
      const n = e.value;
      deco = n !== null && n >= 1 && n <= tr.state.doc.lines ? Decoration.set([Decoration.line({ class: "cm-error-line" }).range(tr.state.doc.line(n).from)]) : Decoration.none;
    }
    return deco;
  },
  provide: (f) => EditorView.decorations.from(f),
});

const theme = EditorView.theme({
  "&": { color: "var(--text)", backgroundColor: "var(--chart)", height: "100%", fontSize: "13px" },
  "&.cm-focused": { outline: "2px solid var(--accent)", outlineOffset: "-2px" },
  ".cm-scroller": { fontFamily: "ui-monospace, 'Cascadia Mono', Consolas, 'SF Mono', Menlo, monospace", lineHeight: "1.5" },
  ".cm-content": { caretColor: "var(--text-strong)" },
  ".cm-cursor, .cm-dropCursor": { borderLeftColor: "var(--text-strong)" },
  ".cm-gutters": { backgroundColor: "var(--panel)", color: "var(--faint)", border: "none", borderRight: "1px solid var(--line)" },
  ".cm-activeLine": { backgroundColor: "color-mix(in srgb, var(--hover) 70%, transparent)" },
  ".cm-activeLineGutter": { backgroundColor: "var(--hover)", color: "var(--text)" },
  ".cm-selectionBackground, &.cm-focused > .cm-scroller > .cm-selectionLayer .cm-selectionBackground": { backgroundColor: "var(--accent-soft)" },
  ".cm-matchingBracket": { backgroundColor: "var(--active)", outline: "1px solid var(--line-strong)" },
  ".cm-error-line": { backgroundColor: "color-mix(in srgb, var(--down) 22%, transparent)" },
});

// Syntax colours from the page's own tokens, so both themes read well.
const highlight = HighlightStyle.define([
  { tag: [tags.keyword, tags.controlKeyword, tags.operatorKeyword, tags.definitionKeyword], color: "var(--accent)" },
  { tag: [tags.string, tags.special(tags.string)], color: "var(--up)" },
  { tag: [tags.number, tags.bool, tags.null], color: "var(--warn)" },
  { tag: [tags.comment, tags.docString], color: "var(--faint)", fontStyle: "italic" },
  { tag: [tags.function(tags.variableName), tags.definition(tags.variableName), tags.className], color: "var(--text-strong)" },
  { tag: [tags.self, tags.standard(tags.variableName)], color: "var(--ask)" },
  { tag: tags.invalid, color: "var(--down)" },
]);

export function createCodeMirror(host: HTMLElement, hooks: EditorHooks, doc: string): CodeEditor {
  const readOnly = new Compartment();
  let quiet = false; // setText must not look like typing
  const view = new EditorView({
    parent: host,
    state: EditorState.create({
      doc,
      extensions: [
        lineNumbers(),
        highlightActiveLineGutter(),
        highlightActiveLine(),
        history(),
        drawSelection(),
        indentOnInput(),
        bracketMatching(),
        python(),
        syntaxHighlighting(highlight),
        errorLine,
        theme,
        EditorView.contentAttributes.of({ "aria-label": "Indicator source code", spellcheck: "false" }),
        readOnly.of([]),
        keymap.of([
          { key: "Mod-s", preventDefault: true, run: () => (hooks.onSave(), true) },
          { key: "Mod-Enter", preventDefault: true, run: () => (hooks.onRun(), true) },
          indentWithTab,
          ...defaultKeymap,
          ...historyKeymap,
        ]),
        EditorView.updateListener.of((u) => {
          if (u.docChanged && !quiet) hooks.onInput(u.state.doc.toString());
        }),
      ],
    }),
  });
  return {
    text: () => view.state.doc.toString(),
    setText(text) {
      quiet = true;
      try {
        view.dispatch({ changes: { from: 0, to: view.state.doc.length, insert: text }, selection: { anchor: 0 } });
      } finally {
        quiet = false;
      }
    },
    focus: () => view.focus(),
    goToLine(line) {
      const n = Math.min(Math.max(1, Math.trunc(line)), view.state.doc.lines);
      const at = view.state.doc.line(n).from;
      view.dispatch({ selection: { anchor: at }, effects: EditorView.scrollIntoView(at, { y: "center" }) });
      view.focus();
    },
    markError: (line) => view.dispatch({ effects: markLine.of(line) }),
    setReadOnly: (ro) => view.dispatch({ effects: readOnly.reconfigure(ro ? [EditorState.readOnly.of(true), EditorView.editable.of(false)] : []) }),
    destroy: () => view.destroy(),
  };
}
