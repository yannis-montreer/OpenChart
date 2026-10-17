/**
 * New, Import, Rename, Duplicate and Export for your own indicator files: the dialogs and the
 * requests. What is checked while typing is in `editor/forms.ts`; the server checks again and a
 * taken name comes back as a 409 with a free one, which is put in the field.
 */

import { ApiError, customApi, type SavedDefinition, type TemplateInfo } from "../api";
import { checkDuplicate, checkImport, checkNew, checkRename, idFromTitle, suggestionText } from "../editor/forms";
import { idFromFilename, normalizeIdInput, sourceProblem } from "../editor/rules";
import { h } from "./dom";
import { openForm, type FormValues } from "./formDialog";
import { toast } from "./toast";

function messageOf(err: unknown): string {
  return err instanceof Error ? err.message : String(err);
}

/** The answer to show in the dialog for a failed request, with the free name a 409 suggests. */
function failure(err: unknown, field = "id"): { error: string; set?: Record<string, string> } {
  if (err instanceof ApiError && err.status === 409) {
    const free = suggestionText(err.detail);
    if (free) return { error: `${err.message} ${free} is free: it is in the field now.`, set: { [field]: free } };
  }
  return { error: messageOf(err) };
}

async function templates(): Promise<TemplateInfo[] | null> {
  try {
    return await customApi.templates();
  } catch (err) {
    toast({ title: "Could not start a new indicator", body: messageOf(err), icon: "warn" });
    return null;
  }
}

export interface Created {
  def: SavedDefinition;
  /** Open it in the editor (New, Import and Duplicate do). */
  edit: boolean;
}

export async function openNewDefinition(opts: { taken: string[]; onDone: (r: Created) => void }): Promise<void> {
  const list = await templates();
  if (!list?.length) return;
  openForm({
    title: "New indicator",
    submit: "Create",
    fields: [
      { name: "template", label: "Start from", kind: "select", value: list[0].id, options: list.map((t) => ({ value: t.id, label: `${t.title}: ${t.description}` })) },
      { name: "title", label: "Title", kind: "text", value: "", placeholder: "SMA cross", maxLength: 60, help: "What the legend and the list show. Leave it empty to use the template's." },
      { name: "id", label: "File name", kind: "text", value: "", placeholder: "sma_cross", maxLength: 40, help: "Lowercase letters, digits and _. It is the name of the .py file and cannot change later." },
    ],
    check: (v) => checkNew({ id: v.text.id, template: v.text.template, title: v.text.title }, list, opts.taken),
    onInput: (name, v, set) => {
      if (name === "title") set("id", idFromTitle(v.text.title));
    },
    send: async (v) => {
      try {
        const def = await customApi.create({ id: normalizeIdInput(v.text.id), template: v.text.template, ...(v.text.title ? { title: v.text.title } : {}) });
        opts.onDone({ def, edit: true });
        return null;
      } catch (err) {
        return failure(err);
      }
    },
  });
}

export function openImportDefinition(opts: { taken: string[]; onDone: (r: Created) => void }): void {
  const file = (v: FormValues) => v.files.file ?? null;
  openForm({
    title: "Import a .py file",
    submit: "Import",
    fields: [
      { name: "file", label: "File", kind: "file", accept: ".py,text/x-python", help: "Up to 256 KB. It is stored as it is; an existing file is never replaced." },
      { name: "id", label: "File name", kind: "text", value: "", placeholder: "made from the file's name", maxLength: 40 },
    ],
    check: (v) => checkImport({ id: v.text.id }, file(v), opts.taken).problems,
    onInput: (name, v, set) => {
      if (name === "file") set("id", idFromFilename(file(v)?.name ?? ""));
    },
    send: async (v) => {
      const f = file(v);
      if (!f) return { error: "Pick a .py file." };
      try {
        const source = await f.text();
        const bad = sourceProblem(source);
        if (bad) return { error: bad };
        const typed = normalizeIdInput(v.text.id);
        const def = await customApi.importSource({ source, filename: f.name, ...(typed ? { id: typed } : {}) });
        opts.onDone({ def, edit: true });
        return null;
      } catch (err) {
        return failure(err);
      }
    },
  });
}

export function openRenameDefinition(opts: { id: string; title: string; onDone: (def: SavedDefinition) => void }): void {
  openForm({
    title: "Rename",
    submit: "Rename",
    fields: [{ name: "title", label: "Title", kind: "text", value: opts.title, maxLength: 60, help: `Only the title changes; the file stays ${opts.id}.py.` }],
    check: (v) => checkRename({ title: v.text.title }, opts.title),
    send: async (v) => {
      try {
        opts.onDone(await customApi.rename(opts.id, v.text.title));
        return null;
      } catch (err) {
        return { error: messageOf(err) };
      }
    },
  });
}

export function openDuplicateDefinition(opts: { id: string; title: string; taken: string[]; onDone: (r: Created) => void }): void {
  openForm({
    title: `Duplicate ${opts.title}`,
    submit: "Duplicate",
    fields: [
      { name: "id", label: "File name", kind: "text", value: "", placeholder: `${opts.id}_2`, maxLength: 40, help: "Empty: the first free name." },
      { name: "title", label: "Title", kind: "text", value: "", placeholder: `${opts.title} (copy)`, maxLength: 60, help: "Empty: the title with (copy)." },
    ],
    check: (v) => checkDuplicate({ id: v.text.id, title: v.text.title }, opts.taken),
    send: async (v) => {
      try {
        const id = normalizeIdInput(v.text.id);
        const def = await customApi.duplicate(opts.id, { ...(id ? { id } : {}), ...(v.text.title ? { title: v.text.title } : {}) });
        opts.onDone({ def, edit: true });
        return null;
      } catch (err) {
        return failure(err);
      }
    },
  });
}

/** Export: the browser downloads the file the server sends (same origin, so the login goes with it). */
export function exportDefinition(id: string): void {
  const a = h("a", { href: customApi.exportPath(id), download: `${id}.py`, hidden: true });
  document.body.append(a);
  a.click();
  a.remove();
}
