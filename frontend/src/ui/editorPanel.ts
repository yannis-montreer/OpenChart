/**
 * The editor panel (docs/ci4-frontend-plan.md): your file's code, Save and Run, the error
 * panel with line links, the versions list, and the notices that say what is going on (editing
 * off, a draft, a conflict, a deleted file). It draws the state of an `EditorSession` and calls
 * its methods; every rule is there. Text that comes from the server or from your code (titles,
 * messages, tracebacks) is only ever set as text, never as markup, and the only links are
 * "line N" buttons that move the cursor.
 */

import { customApi, type PreviewValues } from "../api";
import { openCodeEditor, type CodeEditor } from "../editor/codeEditor";
import { browserDraftStore, type DraftStore } from "../editor/draft";
import { pieces } from "../editor/errors";
import { MAX_SOURCE_BYTES, sourceBytes } from "../editor/rules";
import { EditorSession, type EditorApi, type EditorState, type Gate, type RunQuery } from "../editor/session";
import { confirmDialog } from "./confirm";
import { button, h, icon } from "./dom";

export interface PanelOptions {
  id: string;
  title: string;
  editing: Gate;
  /** The chart's instrument, timeframe and price for Run; null when no chart is showing. */
  query: () => RunQuery | null;
  onPreview: (values: PreviewValues | null, source?: string) => void;
  /** The panel closed (after any question about unsaved text). */
  onClosed: () => void;
  api?: EditorApi;
  store?: DraftStore | null;
}

export interface PanelController {
  readonly id: string;
  /** `indicators_changed` for this file arrived: read it again and settle what differs. */
  changed(op: string): void;
  setEditing(gate: Gate): void;
  setTitle(title: string): void;
  hasUnsaved(): boolean;
  /** Close, asking first when there is unsaved text. Resolves true when it closed. */
  requestClose(): Promise<boolean>;
  focus(): void;
}

const ROOT_CLASS = "editing-code";

function when(ts: number): string {
  return new Date(ts * 1000).toLocaleString([], { year: "numeric", month: "short", day: "numeric", hour: "2-digit", minute: "2-digit", second: "2-digit" });
}

export function openEditorPanel(opts: PanelOptions): PanelController {
  const root = document.getElementById("app") ?? document.body;
  const returnTo = document.activeElement instanceof HTMLElement ? document.activeElement : null;
  const store = opts.store === undefined ? browserDraftStore() : opts.store;
  let session: EditorSession;
  let editor: CodeEditor | null = null;
  let editorKind: "codemirror" | "textarea" | null = null;
  let closing = false;
  let showVersions = false;
  let lastMarked: number | null = null;
  let title = opts.title;

  // Parts

  const titleEl = h("b", { class: "ed-title" });
  const fileEl = h("code", { class: "ed-file" }, `${opts.id}.py`);
  const versionsBtn = h("button", { class: "btn btn-secondary", type: "button", "aria-expanded": "false" }, "Versions", icon("chevron", 14));
  const runBtn = h("button", { class: "btn btn-secondary ed-run", type: "button" }, icon("play", 14), h("span", {}, "Run"));
  const saveBtn = h("button", { class: "btn btn-light ed-save", type: "button" }, icon("save", 14), h("span", {}, "Save"));
  const peekBtn = h("button", { class: "btn btn-secondary ed-peek", type: "button", "aria-label": "Show the chart", "data-tip": "Show the chart" }, icon("chartLine", 14));
  const closeBtn = button({ icon: "x", title: "Close (Esc)" });
  const head = h(
    "div",
    { class: "ed-head" },
    h("div", { class: "ed-heading" }, titleEl, fileEl),
    h("div", { class: "ed-actions" }, versionsBtn, runBtn, saveBtn, peekBtn, closeBtn),
  );
  const notices = h("div", { class: "ed-notices" });
  const host = h("div", { class: "ed-host" }, h("div", { class: "ed-loading" }, "Loading the editor…"));
  const versionsList = h("div", { class: "ed-versions", hidden: true });
  const viewBar = h("div", { class: "ed-viewbar", hidden: true });
  const problems = h("div", { class: "ed-problems", role: "region", "aria-label": "Errors and checks" });
  const status = h("div", { class: "ed-status", role: "status", "aria-live": "polite" });
  const sizeEl = h("span", { class: "ed-size num" });
  const panel = h(
    "aside",
    { class: "editor-panel", "aria-label": "Indicator editor", tabindex: -1 },
    head,
    notices,
    viewBar,
    h("div", { class: "ed-body" }, host, versionsList),
    problems,
    h("div", { class: "ed-foot" }, status, sizeEl),
  );

  // Draw

  const gateTip = (s: EditorState, what: string): string | null => {
    if (!s.editing.allowed) return `${what} is not available: ${s.editing.reason ?? "editing is off"}`;
    if (s.phase !== "ready") return `${what} needs the file to be open.`;
    if (s.problem) return s.problem;
    return null;
  };
  const setBtn = (b: HTMLElement, tip: string | null, idle: string) => {
    b.setAttribute("aria-disabled", String(tip !== null));
    b.setAttribute("data-tip", tip ?? idle);
  };

  const noticeRow = (text: string, ...actions: HTMLElement[]) =>
    h("div", { class: "ed-notice" }, icon("warn", 14), h("span", { class: "ed-notice-text" }, text), ...actions);
  const act = (label: string, run: () => void) => {
    const b = h("button", { class: "btn btn-secondary ed-notice-btn", type: "button" }, label);
    b.addEventListener("click", run);
    return b;
  };

  function renderNotices(s: EditorState): void {
    const rows: HTMLElement[] = [];
    if (!s.editing.allowed) rows.push(noticeRow(`Editing is off: ${s.editing.reason ?? "this server does not allow it"}. You can read the code; to change it, turn editing on in the server's settings.`));
    if (s.phase === "failed" && s.editing.allowed) rows.push(noticeRow(`Could not open this file: ${s.loadError ?? "unknown error"}`, act("Try again", () => void session.open())));
    if (s.phase === "gone") {
      rows.push(
        noticeRow(
          "This file no longer exists on the server. Your text is kept here.",
          ...(s.buffer !== "" && s.editing.allowed ? [act(`Save as ${opts.id}.py`, () => void session.importBuffer())] : []),
        ),
      );
    }
    if (s.draft) {
      rows.push(
        noticeRow(
          `You have unsaved text from ${when(s.draft.at / 1000)}, made from this version of the file.`,
          act("Restore draft", () => session.restoreDraft()),
          act("Discard", () => session.discardDraft()),
        ),
      );
    }
    if (s.conflict) {
      rows.push(
        noticeRow(
          "This file was changed elsewhere (another tab, or a text editor) after you opened it.",
          act("Reload it", () => void confirmThen(s.dirty, "Your text will be replaced by the file as it is now.", () => session.reload())),
          act("Keep mine and save", () => void session.save({ keepMine: true })),
        ),
      );
    }
    notices.replaceChildren(...rows);
  }

  function renderProblems(s: EditorState): void {
    const parts: HTMLElement[] = [];
    const lineCount = s.buffer.split("\n").length;
    const lineButton = (n: number, text: string) => {
      const b = h("button", { class: "ed-line", type: "button" }, text);
      b.addEventListener("click", () => {
        editor?.goToLine(n);
        editor?.markError(n);
        lastMarked = n;
      });
      return b;
    };
    if (s.error) {
      const e = s.error;
      parts.push(
        h(
          "div",
          { class: "ed-error" },
          icon("warn", 14),
          h(
            "div",
            { class: "ed-error-body" },
            h("div", { class: "ed-error-head" }, h("b", {}, e.type), e.message ? `: ${e.message}` : "", " ", ...(e.line && e.line <= lineCount ? [lineButton(e.line, `line ${e.line}`)] : [])),
            ...(e.traceback ? [h("pre", { class: "ed-trace" }, ...pieces(e.traceback, lineCount).map((p) => (p.line ? lineButton(p.line, p.text) : p.text)))] : []),
          ),
        ),
      );
    }
    for (const f of s.findings) {
      parts.push(h("div", { class: `ed-finding ${f.level}` }, icon(f.level === "warning" ? "warn" : "info", 14), h("span", {}, f.text)));
    }
    problems.replaceChildren(...parts);
    problems.hidden = parts.length === 0;
    const mark = s.error?.line ?? null;
    if (mark !== lastMarked) {
      editor?.markError(mark);
      lastMarked = mark;
    }
  }

  function renderVersions(s: EditorState): void {
    versionsList.hidden = !showVersions;
    versionsBtn.setAttribute("aria-expanded", String(showVersions));
    if (!showVersions) return;
    if (!s.versions) {
      versionsList.replaceChildren(h("p", { class: "al-note" }, "Loading…"));
      return;
    }
    if (!s.versions.length) {
      versionsList.replaceChildren(h("p", { class: "al-note" }, "No earlier versions yet. A version is kept each time a save changes the file (the last 10)."));
      return;
    }
    versionsList.replaceChildren(
      h("div", { class: "ed-versions-head" }, "Earlier versions"),
      ...s.versions.map((v) => {
        const view = h("button", { class: "btn btn-secondary", type: "button" }, "View");
        view.addEventListener("click", () => void viewVersion(v.version));
        return h(
          "div",
          { class: `ed-version${s.viewing?.version === v.version ? " current" : ""}` },
          h("span", { class: "ed-version-when" }, when(v.saved_at)),
          h("span", { class: "ed-version-size num" }, `${(v.bytes / 1024).toFixed(1)} KB`),
          view,
        );
      }),
    );
  }

  function renderViewBar(s: EditorState): void {
    viewBar.hidden = !s.viewing;
    if (!s.viewing) return;
    const v = s.viewing;
    const back = h("button", { class: "btn btn-secondary", type: "button" }, "Back to my text");
    back.addEventListener("click", () => leaveView());
    const restore = h("button", { class: "btn btn-light", type: "button" }, "Restore this version");
    restore.setAttribute("aria-disabled", String(!s.editing.allowed));
    restore.addEventListener("click", () => void restoreVersion(v.version));
    viewBar.replaceChildren(icon("history", 14), h("span", {}, `Reading the version of ${when(v.saved_at)}. It is read-only.`), back, restore);
  }

  function render(s: EditorState): void {
    titleEl.textContent = title;
    panel.setAttribute("aria-label", `Indicator editor: ${title}`);
    setBtn(saveBtn, gateTip(s, "Save") ?? (s.saving ? "Saving…" : null), s.dirty ? "Save (Ctrl+S)" : "Save (Ctrl+S): nothing has changed");
    const noChart = opts.query() === null ? "There is no chart to run it on." : null;
    setBtn(runBtn, gateTip(s, "Run") ?? noChart ?? (s.running ? "Running…" : null), "Run on the chart (Ctrl+Enter)");
    runBtn.classList.toggle("busy", s.running);
    saveBtn.classList.toggle("busy", s.saving);
    versionsBtn.toggleAttribute("disabled", s.phase !== "ready");
    renderNotices(s);
    renderProblems(s);
    renderVersions(s);
    renderViewBar(s);
    const bytes = sourceBytes(s.buffer);
    sizeEl.textContent = `${(bytes / 1024).toFixed(1)} of 256 KB`;
    sizeEl.classList.toggle("over", bytes > MAX_SOURCE_BYTES);
    sizeEl.classList.toggle("near", bytes > MAX_SOURCE_BYTES * 0.9);
    const bits = [s.dirty || (s.phase === "gone" && s.buffer !== "") ? "Unsaved changes" : "", s.notice?.text ?? ""].filter(Boolean);
    status.textContent = bits.join(" · ");
    status.classList.toggle("warn", s.notice?.kind === "failed" || s.notice?.kind === "off" || s.notice?.kind === "conflict");
    if (editor && !s.viewing) {
      editor.setReadOnly(!s.editing.allowed || s.phase === "failed");
      // The session replaced the buffer (reload, draft, a change elsewhere, restore): the text box follows.
      if (s.buffer !== editor.text()) editor.setText(s.buffer);
    }
    panel.classList.toggle("dirty", s.dirty);
  }

  // Actions

  async function confirmThen(ask: boolean, text: string, go: () => unknown): Promise<void> {
    if (!ask || (await confirmDialog({ title: "Replace your text?", text, confirm: "Replace", danger: true }))) await go();
  }

  async function viewVersion(version: string): Promise<void> {
    await session.view(version);
    if (session.state.viewing) {
      editor?.setText(session.state.viewing.source);
      editor?.setReadOnly(true);
    }
  }

  function leaveView(): void {
    session.closeView();
    editor?.setText(session.state.buffer);
    editor?.setReadOnly(!session.state.editing.allowed);
  }

  async function restoreVersion(version: string): Promise<void> {
    const ask = session.state.dirty;
    if (!(await confirmDialog({
      title: "Restore this version?",
      text: ask ? "Your unsaved text will be replaced. The file as it is now goes into the versions list first, so this can be undone." : "The file as it is now goes into the versions list first, so this can be undone.",
      confirm: "Restore",
    }))) return;
    await session.restoreVersion(version);
    if (!session.state.viewing) {
      editor?.setText(session.state.buffer);
      editor?.setReadOnly(!session.state.editing.allowed);
    }
  }

  const save = () => void session.save();
  const run = () => {
    const q = opts.query();
    if (q) void session.run({ ...q }); // with no chart the button already says why it cannot
  };

  async function requestClose(): Promise<boolean> {
    if (closing) return false;
    if (session.hasUnsaved()) {
      const keep = await confirmDialog({
        title: "Close with unsaved changes?",
        text: "Your changes are not saved to the file. They stay as a draft in this browser, which you can restore when you open it again.",
        confirm: "Close",
        cancel: "Keep editing",
        danger: true,
      });
      if (!keep) return false;
    }
    closing = true;
    session.close();
    editor?.destroy();
    window.removeEventListener("beforeunload", guard);
    document.removeEventListener("keydown", onKey, true);
    panel.remove();
    root.classList.remove(ROOT_CLASS, "peek");
    opts.onClosed();
    returnTo?.isConnected && returnTo.focus();
    return true;
  }

  const guard = (ev: BeforeUnloadEvent) => {
    if (!session.hasUnsaved()) return;
    ev.preventDefault();
    ev.returnValue = "";
  };
  const onKey = (ev: KeyboardEvent) => {
    if (ev.key !== "Escape" || ev.defaultPrevented) return;
    if (document.querySelector(".menu, .modal-backdrop")) return; // a menu or a dialog above takes the key
    if (!panel.contains(document.activeElement) && document.activeElement !== document.body) return;
    ev.preventDefault();
    void requestClose();
  };

  // Wire

  saveBtn.addEventListener("click", () => {
    if (saveBtn.getAttribute("aria-disabled") !== "true") save();
  });
  runBtn.addEventListener("click", () => {
    if (runBtn.getAttribute("aria-disabled") !== "true") run();
  });
  closeBtn.addEventListener("click", () => void requestClose());
  peekBtn.addEventListener("click", () => {
    const on = root.classList.toggle("peek");
    peekBtn.setAttribute("aria-label", on ? "Show the editor" : "Show the chart");
  });
  versionsBtn.addEventListener("click", () => {
    showVersions = !showVersions;
    if (showVersions) void session.loadVersions();
    render(session.state);
  });

  session = new EditorSession({
    id: opts.id,
    api: opts.api ?? customApi,
    store,
    editing: opts.editing,
    onChange: (s) => render(s),
    onPreview: (values, source) => opts.onPreview(values, source),
  });

  root.classList.add(ROOT_CLASS);
  root.append(panel);
  window.addEventListener("beforeunload", guard);
  document.addEventListener("keydown", onKey, true);
  render(session.state);

  void (async () => {
    await session.open();
    if (closing) return;
    const s = session.state;
    const opened = await openCodeEditor(
      host,
      {
        onInput: (text) => session.edit(text),
        onSave: save,
        onRun: () => {
          if (runBtn.getAttribute("aria-disabled") !== "true") run();
        },
      },
      s.buffer,
      (why) => {
        status.textContent = `The code editor could not be loaded (${why}); a plain text box is used instead.`;
      },
    );
    host.querySelector(".ed-loading")?.remove();
    if (closing) {
      opened.editor.destroy();
      return;
    }
    editor = opened.editor;
    editorKind = opened.kind;
    host.dataset.editor = editorKind;
    editor.setReadOnly(!session.state.editing.allowed || session.state.phase === "failed");
    render(session.state);
    editor.focus();
  })();

  return {
    id: opts.id,
    changed: (op) => void session.refresh(op),
    setEditing: (gate) => session.setEditing(gate),
    setTitle(next) {
      title = next;
      render(session.state);
    },
    hasUnsaved: () => session.hasUnsaved(),
    requestClose,
    focus: () => (editor ? editor.focus() : panel.focus()),
  };
}
