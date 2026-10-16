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

/** A modal with a title, a close button and Escape / backdrop to close. */
function modal(cls: string, title: string, onClose: () => void) {
  const box = h("div", { class: `modal ${cls}`, role: "dialog", "aria-modal": "true", "aria-label": title });
  const backdrop = h("div", { class: "modal-backdrop" }, box);
  const onKey = (ev: KeyboardEvent) => {
    if (ev.key === "Escape" && !document.querySelector(".menu")) {
      ev.stopPropagation();
      close();
    }
  };
  const close = () => {
    backdrop.remove();
    document.removeEventListener("keydown", onKey, true);
    onClose();
  };
  backdrop.addEventListener("pointerdown", (ev) => {
    if (ev.target === backdrop) close();
  });
  document.addEventListener("keydown", onKey, true);
  const head = h("div", { class: "modal-head" }, h("h2", {}, title), button({ icon: "x", title: "Close (Esc)", onClick: () => close() }));
  box.append(head);
  document.body.append(backdrop);
  return { box, close };
}

// Indicators list

export function openIndicatorPicker(opts: { catalog: IndicatorCatalog; onPick: (def: IndicatorDef) => void }): void {
  const { box, close } = modal("ind-picker", "Indicators", () => {});
  const search = h("input", { class: "input", type: "search", placeholder: "Search", spellcheck: "false", "aria-label": "Search indicators" });
  const list = h("div", { class: "ind-list", role: "listbox" });
  const render = () => {
    const q = search.value.trim().toLowerCase();
    const found = opts.catalog.indicators.filter((d) => `${d.name} ${d.title}`.toLowerCase().includes(q));
    list.replaceChildren(
      ...found.map((d) => {
        const row = h(
          "button",
          { class: "ind-item", type: "button", role: "option" },
          icon("indicators", 16),
          h("span", { class: "ind-title" }, d.title),
          h("span", { class: "ind-tag" }, d.name),
          d.builtin ? null : h("span", { class: "ind-own", "data-tip": "From your indicators folder" }, "your file"),
          h("span", { class: "ind-where" }, d.overlay ? "on the chart" : "own pane"),
        );
        row.addEventListener("click", () => {
          opts.onPick(d);
          close();
        });
        return row;
      }),
    );
    if (!found.length) list.append(h("div", { class: "panel-empty" }, "No indicator matches."));
  };
  search.addEventListener("input", render);
  search.addEventListener("keydown", (ev) => {
    if (ev.key === "Enter") (list.querySelector(".ind-item") as HTMLButtonElement | null)?.click();
  });
  const errors = opts.catalog.errors.length
    ? h(
        "div",
        { class: "ind-errors" },
        h("div", { class: "menu-section" }, "Files that did not load"),
        ...opts.catalog.errors.map((e) => h("div", { class: "ind-error" }, icon("warn", 14), h("b", {}, e.file), h("span", {}, e.error))),
      )
    : null;
  // How isolated your files are on this install (they run in the indicator worker).
  const notice = opts.catalog.isolation?.notice;
  box.append(
    h("div", { class: "ind-search" }, icon("search", 16), search),
    list,
    errors ?? "",
    notice ? h("div", { class: "ind-error ind-notice" }, icon("warn", 14), h("span", {}, notice)) : "",
    // What the worker can still do here, for example its own folder (owner's decision B1).
    ...(opts.catalog.isolation?.notes ?? [])
      .filter((n) => n.startsWith("its own folder"))
      .map((n) => h("div", { class: "ind-error ind-notice" }, icon("warn", 14), h("span", {}, n))),
    h(
      "div",
      { class: "ind-foot" },
      "Add your own: put a Python file in ",
      h("code", {}, opts.catalog.folder),
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
