/**
 * A small form in a dialog, for New, Rename, Duplicate and Import. It knows nothing of what the
 * fields mean: `check` says what is wrong with the values (shown under each field as they are
 * typed, and the button waits until nothing is), `submit` makes the request and may answer with
 * an error to show and values to put back (a taken name, with a free one).
 */

import { h } from "./dom";
import { resultLine } from "./forms";
import { modal } from "./modal";

export type FieldSpec =
  | { name: string; label: string; kind: "text"; value: string; placeholder?: string; help?: string; maxLength?: number }
  | { name: string; label: string; kind: "select"; value: string; options: { value: string; label: string }[]; help?: string }
  | { name: string; label: string; kind: "file"; accept: string; help?: string };

export interface FormValues {
  text: Record<string, string>;
  files: Record<string, File | null>;
}

export interface FormSpec {
  title: string;
  submit: string;
  fields: FieldSpec[];
  /** Problems by field name, from the current values. */
  check: (v: FormValues) => Record<string, string | undefined>;
  /** Make the request. Throw nothing: answer what to show instead. `null` closes the dialog. */
  send: (v: FormValues) => Promise<null | { error: string; set?: Record<string, string> }>;
  /** A value changed in this field: other fields may follow (an id from a title). */
  onInput?: (name: string, v: FormValues, set: (name: string, value: string) => void) => void;
}

export function openForm(spec: FormSpec): void {
  const { box, close } = modal("ind-form", spec.title, () => {});
  const texts = new Map<string, HTMLInputElement | HTMLSelectElement>();
  const files = new Map<string, HTMLInputElement>();
  const notes = new Map<string, HTMLElement>();
  const touched = new Set<string>();
  const result = resultLine();
  const submit = h("button", { class: "btn btn-light", type: "submit" }, spec.submit);
  const values = (): FormValues => ({
    text: Object.fromEntries([...texts].map(([k, el]) => [k, el.value])),
    files: Object.fromEntries([...files].map(([k, el]) => [k, el.files?.[0] ?? null])),
  });

  const rows = spec.fields.map((f) => {
    let control: HTMLInputElement | HTMLSelectElement;
    if (f.kind === "select") {
      control = h("select", { class: "input", name: f.name }, ...f.options.map((o) => h("option", { value: o.value }, o.label)));
      control.value = f.value;
      texts.set(f.name, control);
    } else if (f.kind === "file") {
      control = h("input", { class: "input", type: "file", accept: f.accept, name: f.name });
      files.set(f.name, control);
    } else {
      control = h("input", { class: "input", type: "text", name: f.name, spellcheck: "false", autocomplete: "off", maxLength: f.maxLength ?? 100 });
      control.value = f.value;
      if (f.placeholder) control.placeholder = f.placeholder;
      texts.set(f.name, control);
    }
    const note = h("small", { class: "ind-problem", "aria-live": "polite" });
    notes.set(f.name, note);
    control.addEventListener("input", () => {
      touched.add(f.name);
      spec.onInput?.(f.name, values(), (name, value) => {
        const other = texts.get(name);
        if (other && !touched.has(name)) other.value = value;
      });
      refresh();
    });
    return h(
      "div",
      { class: "al-row" },
      h("label", { class: "al-label" }, f.label),
      h("div", { class: "al-value" }, control, f.help ? h("small", { class: "al-note" }, f.help) : null, note),
    );
  });

  const refresh = () => {
    const problems = spec.check(values());
    for (const [name, note] of notes) note.textContent = touched.has(name) ? (problems[name] ?? "") : "";
    const blocked = Object.values(problems).some(Boolean);
    submit.setAttribute("aria-disabled", String(blocked));
    return blocked;
  };

  const form = h("form", { class: "ind-form-body", novalidate: true }, ...rows, result.el);
  const cancel = h("button", { class: "btn btn-secondary", type: "button" }, "Cancel");
  cancel.addEventListener("click", close);
  form.append(h("div", { class: "al-foot" }, h("div", { class: "spacer" }), cancel, submit));
  let busy = false;
  form.addEventListener("submit", (ev) => {
    ev.preventDefault();
    if (busy) return;
    for (const f of spec.fields) touched.add(f.name);
    if (refresh()) return;
    busy = true;
    submit.setAttribute("aria-disabled", "true");
    result.clear();
    void spec.send(values()).then((answer) => {
      busy = false;
      if (answer === null) {
        close();
        return;
      }
      for (const [name, value] of Object.entries(answer.set ?? {})) {
        const el = texts.get(name);
        if (el) el.value = value;
      }
      result.fail(answer.error);
      refresh();
    });
  });
  box.append(form);
  refresh();
  (form.querySelector("input, select") as HTMLElement | null)?.focus();
}
