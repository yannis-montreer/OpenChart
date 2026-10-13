/** Form building blocks for the setup wizard, login screen and settings dialog. */

import { h, icon, type IconName } from "./dom";

export function field(label: string, control: HTMLElement, hint?: string | Node): HTMLElement {
  return h(
    "label",
    { class: "field" },
    h("span", { class: "field-label" }, label),
    control,
    hint ? h("span", { class: "field-hint" }, hint) : null,
  );
}

export function textInput(opts: {
  value?: string;
  placeholder?: string;
  type?: string;
  autocomplete?: string;
  name?: string;
}): HTMLInputElement {
  const input = h("input", {
    class: "input",
    type: opts.type ?? "text",
    placeholder: opts.placeholder ?? "",
    autocomplete: opts.autocomplete ?? "off",
    spellcheck: "false",
    name: opts.name,
  });
  input.value = opts.value ?? "";
  return input;
}

/** Password-style input with a show/hide button. */
export function secretInput(opts: { placeholder?: string; autocomplete?: string; name?: string }): {
  el: HTMLElement;
  input: HTMLInputElement;
} {
  const input = textInput({ ...opts, type: "password", autocomplete: opts.autocomplete ?? "off" });
  const toggle = h(
    "button",
    { class: "input-eye", type: "button", "aria-label": "Show", tabindex: "-1" },
    icon("eye", 16),
  );
  toggle.addEventListener("click", () => {
    const show = input.type === "password";
    input.type = show ? "text" : "password";
    toggle.replaceChildren(icon(show ? "eyeOff" : "eye", 16));
    toggle.setAttribute("aria-label", show ? "Hide" : "Show");
  });
  return { el: h("div", { class: "input-wrap" }, input, toggle), input };
}

export function segmented<T extends string>(
  options: [T, string][],
  value: T,
  onChange?: (v: T) => void,
): { el: HTMLElement; value: () => T; set: (v: T) => void } {
  let current = value;
  const el = h("div", { class: "seg seg-lg", role: "radiogroup" });
  const render = () => {
    el.replaceChildren(
      ...options.map(([v, label]) => {
        const b = h(
          "button",
          { type: "button", class: `btn${v === current ? " active" : ""}`, role: "radio", "aria-checked": String(v === current) },
          label,
        );
        b.addEventListener("click", () => {
          current = v;
          render();
          onChange?.(v);
        });
        return b;
      }),
    );
  };
  render();
  return {
    el,
    value: () => current,
    set: (v) => {
      current = v;
      render();
    },
  };
}

export function toggleSwitch(
  label: string,
  checked: boolean,
  hint?: string,
): { el: HTMLElement; input: HTMLInputElement } {
  const input = h("input", { type: "checkbox", class: "switch-input" });
  input.checked = checked;
  return {
    el: h(
      "label",
      { class: "switch-row" },
      h(
        "span",
        { class: "switch-text" },
        h("span", { class: "field-label" }, label),
        hint ? h("span", { class: "field-hint" }, hint) : null,
      ),
      input,
      h("span", { class: "switch", "aria-hidden": "true" }),
    ),
    input,
  };
}

export interface ResultLine {
  el: HTMLElement;
  busy: (text?: string) => void;
  ok: (text: string) => void;
  fail: (text: string) => void;
  clear: () => void;
}

/** A line under a Test/Save button: spinner, green success or red error. */
export function resultLine(): ResultLine {
  const el = h("div", { class: "result", role: "status", "aria-live": "polite" });
  const set = (cls: string, ic: IconName, text: string) => {
    el.className = `result ${cls}`;
    el.replaceChildren(icon(ic, 16), h("span", {}, text));
  };
  return {
    el,
    busy: (text = "Working…") => set("busy", "loader", text),
    ok: (text) => set("ok", "ok", text),
    fail: (text) => set("fail", "fail", text),
    clear: () => {
      el.className = "result";
      el.replaceChildren();
    },
  };
}

export function primary(label: string, onClick?: () => void): HTMLButtonElement {
  const b = h("button", { class: "btn btn-primary", type: "button" }, label);
  if (onClick) b.addEventListener("click", onClick);
  return b;
}

export function secondary(label: string, onClick?: () => void, ic?: IconName): HTMLButtonElement {
  const b = h("button", { class: "btn btn-secondary", type: "button" }, ...(ic ? [icon(ic, 16)] : []), label);
  if (onClick) b.addEventListener("click", onClick);
  return b;
}

/** Disable `button` while `fn` runs. */
export async function withBusy<T>(button: HTMLButtonElement, fn: () => Promise<T>): Promise<T | undefined> {
  button.disabled = true;
  try {
    return await fn();
  } finally {
    button.disabled = false;
  }
}
