/**
 * The indicators list and the indicator settings dialog. Both are built from the schemas
 * the server sends (GET /api/indicators), so a new indicator file needs no UI work: its
 * parameters become fields on the Inputs tab, its colours and widths on the Style tab.
 */

import type { IndicatorCatalog, IndicatorDef, IndicatorEntry, IndicatorParam, ParamValue } from "../api";
import { outputWidth } from "../chart/indicatorOutputs";
import { paramProblem } from "../chart/params";
import { indicatorLabel } from "../chart/indicators";
import { PALETTE } from "../drawings/toolbar";
import { button, closeMenu, h, icon, menuItem, showMenu } from "./dom";
import { openDeleteDefinition } from "./deleteDefinition";
import { modal } from "./modal";
import { pickerModel, type PickerRow, type RowState } from "./pickerModel";

const SOURCE_LABELS: Record<string, string> = {
  open: "Open",
  high: "High",
  low: "Low",
  close: "Close",
  hl2: "(H + L) / 2",
  hlc3: "(H + L + C) / 3",
  ohlc4: "(O + H + L + C) / 4",
  hlcc4: "(H + L + C + C) / 4",
};

// Indicators list

/** Short words for the state of a file of yours that cannot be added (the reason is under the name). */
const STATE_LABELS: Partial<Record<RowState, string>> = {
  error: "did not load",
  quarantined: "switched off",
  not_run: "worker not running",
  shadowed: "id taken",
};

/** What the list can do with your own files (CI-4); left out, the list only adds and deletes. */
export interface DefinitionHandlers {
  edit: (id: string) => void;
  create: () => void;
  import: () => void;
  rename: (id: string, title: string) => void;
  duplicate: (id: string, title: string) => void;
  export: (id: string) => void;
}

/** A button that stays focusable when it cannot be used, so its tooltip can say why. */
function gated(label: string, gate: { allowed: boolean; reason: string | null }, ic: Parameters<typeof button>[0]["icon"], run: () => void, cls = ""): HTMLButtonElement {
  const b = button({ icon: ic, title: gate.allowed ? label : `${label} is not available: ${gate.reason}`, class: `${cls}${gate.allowed ? "" : " ind-locked"}` });
  if (!gate.allowed) b.setAttribute("aria-disabled", "true");
  b.addEventListener("click", () => {
    if (gate.allowed) run();
  });
  return b;
}

function pickerRow(
  row: PickerRow,
  onPick: (def: IndicatorDef) => void,
  close: () => void,
  onDelete: (row: PickerRow) => void,
  defs?: DefinitionHandlers,
): HTMLElement {
  const lead = icon(row.addable ? "indicators" : "warn", 16);
  const head = h(
    "span",
    { class: "ind-head" },
    h("span", { class: "ind-title" }, row.title),
    row.name === row.title ? null : h("span", { class: "ind-tag" }, row.name),
    row.file ? h("span", { class: "ind-own", "data-tip": "From your indicators folder" }, row.file) : null,
    row.addable ? null : h("span", { class: `ind-state ind-state-${row.state}` }, STATE_LABELS[row.state] ?? row.state),
  );
  const text = h("span", { class: "ind-text" }, head, row.why ? h("span", { class: "ind-why" }, row.line ? `line ${row.line}: ${row.why}` : row.why) : null);
  const where = h("span", { class: "ind-where" }, row.where ?? "");
  let main: HTMLElement;
  if (row.def) {
    const def = row.def;
    main = h("button", { class: "ind-item", type: "button", role: "option" }, lead, text, where);
    main.addEventListener("click", () => {
      onPick(def);
      close();
    });
  } else {
    main = h("div", { class: "ind-item ind-disabled", role: "option", "aria-disabled": "true" }, lead, text, where);
  }
  const wrap = h("div", { class: "ind-row" }, main);
  if (defs && row.actions) {
    const a = row.actions;
    const more = button({ icon: "more", title: `More for ${row.title}`, class: "ind-more" });
    more.addEventListener("click", () =>
      showMenu(
        more,
        h(
          "div",
          { style: "min-width:190px" },
          ...[
            ["Rename…", "pencil", a.rename, () => defs.rename(row.id, row.title)],
            ["Duplicate…", "copy", a.duplicate, () => defs.duplicate(row.id, row.title)],
            ["Export (.py)", "save", a.export, () => defs.export(row.id)],
          ].map(([label, ic, gate, run]) => {
            const g = gate as { allowed: boolean; reason: string | null };
            const item = menuItem({ label: label as string, icon: ic as "pencil", onClick: g.allowed ? (run as () => void) : undefined });
            if (!g.allowed) {
              item.setAttribute("aria-disabled", "true");
              item.setAttribute("data-tip", `Not available: ${g.reason}`);
            }
            return item;
          }),
        ),
      ),
    );
    wrap.append(
      gated(`Edit ${row.title}`, a.edit, "pencil", () => {
        close();
        defs.edit(row.id);
      }, "ind-edit"),
      more,
    );
  }
  if (row.canDelete) {
    const gate = row.canDelete;
    const del = button({
      icon: "trash",
      title: gate.allowed ? `Delete ${row.title}…` : `Deleting is not available: ${gate.reason}`,
      class: `ind-del${gate.allowed ? "" : " ind-locked"}`,
    });
    // Not `disabled`: a disabled button shows no tooltip, and the reason is the point.
    if (!gate.allowed) del.setAttribute("aria-disabled", "true");
    del.addEventListener("click", () => {
      if (gate.allowed) onDelete(row);
    });
    wrap.append(del);
  }
  return wrap;
}

export function openIndicatorPicker(opts: {
  catalog: IndicatorCatalog;
  onPick: (def: IndicatorDef) => void;
  /** Read the catalog again (after a delete, or when a delete failed in a way that leaves it unknown). */
  reload?: () => Promise<IndicatorCatalog>;
  /** A definition of yours was deleted: charts that still show it should drop it. */
  onDeleted?: (id: string) => void;
  /** Edit, New, Import, Rename, Duplicate and Export (CI-4). */
  definitions?: DefinitionHandlers;
}): void {
  const { box, close } = modal("ind-picker", "Indicators", () => {});
  let catalog = opts.catalog;
  const search = h("input", { class: "input", type: "search", placeholder: "Search", spellcheck: "false", "aria-label": "Search indicators" });
  const list = h("div", { class: "ind-list", role: "listbox" });
  const render = () => {
    const model = pickerModel(catalog, search.value);
    const onDelete = (row: PickerRow) =>
      openDeleteDefinition({
        id: row.id,
        title: row.title,
        onChanged: () => {
          void opts.reload?.().then((fresh) => {
            catalog = fresh;
            // Only what the server no longer has is dropped from the charts: a failed delete
            // leaves it, and a half-done one (the file moved, the answer was lost) is caught here.
            const there = (fresh.custom ?? []).some((e) => e.id === row.id) || fresh.indicators.some((d) => d.id === row.id && !d.builtin);
            if (!there) opts.onDeleted?.(row.id);
            render();
          });
        },
      });
    const nodes: HTMLElement[] = [];
    for (const s of model.sections) {
      nodes.push(h("div", { class: "ind-section-head" }, s.heading));
      for (const n of s.notices) nodes.push(h("div", { class: "ind-error ind-notice" }, icon("warn", 14), h("span", {}, n)));
      if (opts.definitions && s.editNote) nodes.push(h("div", { class: "ind-error ind-notice" }, icon("warn", 14), h("span", {}, s.editNote)));
      for (const r of s.rows) nodes.push(pickerRow(r, opts.onPick, close, onDelete, opts.definitions));
      if (s.hint) nodes.push(h("div", { class: "panel-empty" }, s.hint));
    }
    if (model.empty) nodes.push(h("div", { class: "panel-empty" }, model.empty));
    list.replaceChildren(...nodes);
  };
  const defs = opts.definitions;
  const tools = defs
    ? h(
        "div",
        { class: "ind-tools" },
        h("span", { class: "ind-tools-label" }, "Your files"),
        ...[
          ["New", "plus", () => defs.create()],
          ["Import", "save", () => defs.import()],
        ].map(([label, ic, run]) => {
          const g = pickerModel(catalog, "").create;
          const b = gated(`${label as string} indicator`, g, ic as "plus", () => {
            close();
            (run as () => void)();
          }, "ind-tool");
          b.append(h("span", {}, label as string));
          b.classList.remove("icon-only");
          return b;
        }),
      )
    : null;
  search.addEventListener("input", render);
  search.addEventListener("keydown", (ev) => {
    if (ev.key === "Enter") (list.querySelector("button.ind-item") as HTMLButtonElement | null)?.click();
  });
  box.append(
    h("div", { class: "ind-search" }, icon("search", 16), search),
    ...(tools ? [tools] : []),
    list,
    h(
      "div",
      { class: "ind-foot" },
      "Add your own: put a Python file in ",
      h("code", {}, catalog.folder),
      " (no restart needed). See docs/indicators.md.",
    ),
  );
  render();
  search.focus();
}

// Settings

export interface SettingsOptions {
  def: IndicatorDef;
  entry: IndicatorEntry;
  /** Every change, for a live preview. */
  onPreview: (entry: IndicatorEntry) => void;
  /** Ok: keep it. */
  onApply: (entry: IndicatorEntry) => void;
  /** Cancel (or close): back to `entry` as it was. */
  onCancel: (entry: IndicatorEntry) => void;
}

export { paramProblem };

export function openIndicatorSettings(opts: SettingsOptions): void {
  const { def } = opts;
  const original = structuredClone(opts.entry);
  let entry = structuredClone(opts.entry);
  let done = false;
  const problems = new Map<string, string>();
  const { box, close } = modal("ind-settings", def.name, () => {
    if (!done) opts.onCancel(original);
  });

  const ok = h("button", { class: "btn btn-light", type: "button" }, "Ok");
  const changed = () => {
    ok.disabled = problems.size > 0;
    if (!problems.size) opts.onPreview(structuredClone(entry));
  };
  const setParam = (name: string, value: ParamValue) => {
    entry = { ...entry, params: { ...entry.params, [name]: value } };
    changed();
  };

  // Inputs tab
  const inputs = h("div", { class: "ind-tab" });
  const renderInputs = () => {
    inputs.replaceChildren();
    for (const p of def.params.filter((x) => x.group === "inputs")) inputs.append(inputRow(p));
    if (!inputs.childElementCount) inputs.append(h("p", { class: "al-note" }, "This indicator has no inputs."));
  };
  const inputRow = (p: IndicatorParam): HTMLElement => {
    const value = entry.params[p.name] ?? p.default;
    const note = h("small", { class: "ind-problem" });
    let control: HTMLElement;
    if (p.type === "bool") {
      const box = h("input", { type: "checkbox" });
      box.checked = Boolean(value);
      box.addEventListener("change", () => setParam(p.name, box.checked));
      return h("label", { class: "check ind-check" }, box, h("span", {}, p.label));
    }
    if (p.type === "source" || p.type === "choice") {
      const sel = h("select", { class: "input" });
      for (const c of p.choices ?? []) sel.append(h("option", { value: c, selected: c === value }, p.type === "source" ? (SOURCE_LABELS[c] ?? c) : c));
      sel.addEventListener("change", () => setParam(p.name, sel.value));
      control = sel;
    } else if (p.type === "string") {
      const text = h("input", { class: "input", type: "text", maxLength: p.max_length ?? 100 });
      text.value = String(value);
      text.addEventListener("input", () => {
        const problem = paramProblem(p, text.value);
        note.textContent = problem ?? "";
        if (problem) problems.set(p.name, problem);
        else problems.delete(p.name);
        if (!problem) setParam(p.name, text.value);
        else changed();
      });
      control = text;
    } else {
      const num = h("input", { class: "input num", type: "number", inputmode: p.type === "float" ? "decimal" : "numeric", step: String(p.step ?? "any") });
      if (p.min !== undefined) num.min = String(p.min);
      if (p.max !== undefined) num.max = String(p.max);
      num.value = String(value);
      num.addEventListener("input", () => {
        const v = num.value.trim() === "" ? NaN : Number(num.value);
        const problem = paramProblem(p, v);
        note.textContent = problem ?? "";
        num.classList.toggle("invalid", problem !== null);
        if (problem) problems.set(p.name, problem);
        else problems.delete(p.name);
        if (!problem) setParam(p.name, v);
        else changed();
      });
      control = num;
    }
    return h("div", { class: "al-row" }, h("label", { class: "al-label" }, p.label), h("div", { class: "al-value" }, control, note));
  };

  // Style tab
  const style = h("div", { class: "ind-tab" });
  const renderStyle = () => {
    style.replaceChildren();
    const used = new Set<string>();
    for (const o of def.outputs) {
      used.add(o.color);
      const width = outputWidth(o); // histograms, fills, markers and backgrounds have none
      if (width) used.add(width);
      style.append(
        h(
          "div",
          { class: "al-row" },
          h("span", { class: "al-label" }, o.label),
          h(
            "div",
            { class: "ind-style" },
            colorButton(String(entry.params[o.color]), (c) => setParam(o.color, c)),
            ...(width ? [widthSelect(Number(entry.params[width]), (w) => setParam(width, w))] : []),
          ),
        ),
      );
    }
    for (const p of def.params.filter((x) => x.group === "style" && !used.has(x.name))) {
      const control =
        p.type === "color"
          ? colorButton(String(entry.params[p.name]), (c) => setParam(p.name, c))
          : widthSelect(Number(entry.params[p.name]), (w) => setParam(p.name, w));
      style.append(h("div", { class: "al-row" }, h("span", { class: "al-label" }, p.label), h("div", { class: "ind-style" }, control)));
    }
    const flag = (key: "price_labels" | "legend_values", label: string) => {
      const box = h("input", { type: "checkbox" });
      box.checked = entry[key];
      box.addEventListener("change", () => {
        entry = { ...entry, [key]: box.checked };
        changed();
      });
      return h("label", { class: "check ind-check" }, box, h("span", {}, label));
    };
    style.append(
      h("div", { class: "menu-section ind-section" }, "Output values"),
      flag("price_labels", "Labels on price scale"),
      flag("legend_values", "Values in status line"),
    );
  };

  // Tabs, footer
  const tabs = h("div", { class: "ind-tabs", role: "tablist" });
  const body = h("div", { class: "al-body ind-body" });
  let tab: "inputs" | "style" = "inputs";
  const renderTabs = () => {
    tabs.replaceChildren(
      ...(["inputs", "style"] as const).map((t) => {
        const b = h("button", { type: "button", role: "tab", class: `ind-tab-btn${t === tab ? " active" : ""}`, "aria-selected": String(t === tab) }, t === "inputs" ? "Inputs" : "Style");
        b.addEventListener("click", () => {
          tab = t;
          renderTabs();
        });
        return b;
      }),
    );
    body.replaceChildren(tab === "inputs" ? inputs : style);
  };
  const defaults = h("button", { class: "btn btn-secondary ind-defaults", type: "button" }, h("span", {}, "Defaults"), icon("chevron", 14));
  defaults.addEventListener("click", () =>
    showMenu(
      defaults,
      h(
        "div",
        {},
        menuItem({
          label: "Reset settings",
          onClick: () => {
            const params = Object.fromEntries(def.params.map((p) => [p.name, p.default]));
            entry = { ...entry, params, price_labels: true, legend_values: true };
            problems.clear();
            renderInputs();
            renderStyle();
            changed();
          },
        }),
      ),
    ),
  );
  const cancel = h("button", { class: "btn btn-secondary", type: "button" }, "Cancel");
  cancel.addEventListener("click", () => close());
  ok.addEventListener("click", () => {
    done = true;
    opts.onApply(structuredClone(entry));
    close();
  });
  box.addEventListener("keydown", (ev) => {
    if (ev.key === "Enter" && (ev.target as HTMLElement).tagName === "INPUT" && !ok.disabled) ok.click();
  });
  box.append(tabs, body, h("div", { class: "al-foot" }, defaults, h("div", { class: "spacer" }), cancel, ok));
  box.setAttribute("aria-label", `${indicatorLabel(def, entry.params)} settings`);
  renderInputs();
  renderStyle();
  renderTabs();
  (inputs.querySelector("input, select") as HTMLElement | null)?.focus();
}

function colorButton(value: string, onPick: (c: string) => void): HTMLButtonElement {
  const btn = h("button", { class: "btn ind-color", type: "button", "aria-label": "Colour" }, h("span", { class: "swatch", style: `background:${value}` }));
  const set = (c: string) => {
    btn.firstElementChild!.setAttribute("style", `background:${c}`);
    onPick(c);
  };
  btn.addEventListener("click", () => {
    const grid = h("div", { class: "palette" });
    for (const c of PALETTE) {
      const b = h("button", { type: "button", class: "palette-swatch", style: `background:${c}`, "aria-label": c });
      b.addEventListener("click", () => {
        set(c);
        closeMenu();
      });
      grid.append(b);
    }
    const custom = h("input", { type: "color", class: "palette-custom", value, title: "Other colour" });
    custom.addEventListener("change", () => set(custom.value));
    showMenu(btn, h("div", {}, grid, h("hr"), h("label", { class: "palette-row" }, "Other colour", custom)));
  });
  return btn;
}

function widthSelect(value: number, onPick: (w: number) => void): HTMLSelectElement {
  const sel = h("select", { class: "input ind-width", "aria-label": "Line width" });
  for (const w of [1, 2, 3, 4]) sel.append(h("option", { value: String(w), selected: w === value }, `${w}px`));
  sel.addEventListener("change", () => onPick(Number(sel.value)));
  return sel;
}
