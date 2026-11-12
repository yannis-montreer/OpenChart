/**
 * Create / edit an alert on one indicator of the chart (docs/designs/indicator-alerts.md).
 * Opened from the bell on the indicator's legend row and from the Alerts panel. The rules
 * (which outputs fit which condition, what the form must be) are in `alerts/indicatorAlerts.ts`;
 * the server has the last word and its errors are shown in words.
 */

import {
  alertsApi,
  type Alert,
  type AlertChannel,
  type AlertDirection,
  type AlertFrequency,
  type AlertInput,
  type IndicatorDef,
  type Price,
} from "../api";
import {
  availableConditions,
  buildIndicatorInput,
  closedBarNote,
  crossableOutputs,
  nameToSend,
  otherOutputs,
  problemText,
  signalOutputs,
  validateForm,
  viewOf,
  warningText,
  type Condition,
  type IndicatorForm,
  type OutputChoice,
} from "../alerts/indicatorAlerts";
import { confirmAlertDelete } from "./alertDialog";
import { button, h } from "./dom";
import { resultLine } from "./forms";

const DAY = 86400;

export interface IndicatorAlertDialogOptions {
  instrument: string;
  /** The chart's; seconds charts alert on 1m bars (indicator alerts need closed bars of a minute or more). */
  timeframe: string;
  price: Price;
  def: IndicatorDef;
  /** The instance on the chart. */
  uid: string;
  /** "RSI(14)": the name and its value inputs, as the legend shows. */
  title: string;
  alert?: Alert;
  telegramReady: boolean;
  onSaved?: (alert: Alert) => void;
  onDeleted?: (alert: Alert) => void;
}

const DIRECTIONS: { id: AlertDirection; label: string }[] = [
  { id: "cross", label: "Crossing" },
  { id: "up", label: "Crossing Up" },
  { id: "down", label: "Crossing Down" },
];

interface Option {
  value: string;
  label: string;
  disabled?: boolean;
  title?: string;
}

function select(label: string, options: Option[], value: string): HTMLSelectElement {
  const el = h("select", { class: "input", "aria-label": label });
  for (const o of options) {
    const opt = h("option", { value: o.value, disabled: o.disabled, title: o.title }, o.label);
    el.append(opt);
  }
  el.value = value;
  return el;
}

export function openIndicatorAlertDialog(opts: IndicatorAlertDialogOptions): void {
  const { def, alert } = opts;
  const editing = alert !== undefined;
  const timeframe = editing ? alert.timeframe : opts.timeframe.endsWith("s") ? "1m" : opts.timeframe;
  const spec = alert?.indicator ?? null;
  const conditions = availableConditions(def);
  const firstEnabled = conditions.find((c) => c.enabled)?.id ?? "cross_value";

  let condition: Condition = spec?.condition ?? firstEnabled;
  const notify = new Set<AlertChannel>(alert?.notify ?? ["telegram", "app"]);

  const choices = (c: Condition): OutputChoice[] => (c === "signal" ? signalOutputs(def) : crossableOutputs(def));
  const toOptions = (list: OutputChoice[]): Option[] => list.map((o) => ({ value: o.name, label: o.label === o.name ? o.name : `${o.label} (${o.name})` }));

  const conditionSel = select(
    "Condition",
    conditions.map((c) => ({ value: c.id, label: c.label, disabled: !c.enabled, title: c.why })),
    condition,
  );
  const outputSel = select("Output", toOptions(choices(condition)), spec?.output ?? "");
  const directionSel = select("Direction", DIRECTIONS.map((d) => ({ value: d.id, label: d.label })), alert?.direction ?? "cross");
  const numberInput = h("input", { class: "input num", type: "text", inputmode: "decimal", "aria-label": "Number", spellcheck: "false" });
  numberInput.value = spec?.value !== null && spec?.value !== undefined ? String(spec.value) : "";
  const otherSel = select("Second output", [], "");
  const refillOther = () => {
    const list = otherOutputs(def, outputSel.value);
    otherSel.replaceChildren(...toOptions(list).map((o) => h("option", { value: o.value }, o.label)));
    const want = spec?.other_output ?? "";
    otherSel.value = list.some((o) => o.name === want) ? want : (list[0]?.name ?? "");
  };
  const refillOutputs = (keep: string) => {
    const list = choices(condition);
    outputSel.replaceChildren(...toOptions(list).map((o) => h("option", { value: o.value }, o.label)));
    outputSel.value = list.some((o) => o.name === keep) ? keep : (list[0]?.name ?? "");
  };
  refillOutputs(spec?.output ?? "");
  refillOther();

  const freqSel = select(
    "Frequency",
    [
      { value: "once", label: "Once only", title: "Fires once, then stops" },
      { value: "once_per_bar", label: "Every time it happens", title: "Fires on every closed bar where the condition holds" },
    ],
    alert?.frequency === "once" || !alert ? "once" : "once_per_bar",
  );
  const priceSel = select("Price", (["bid", "mid", "ask"] as Price[]).map((p) => ({ value: p, label: p[0].toUpperCase() + p.slice(1) })), alert?.trigger_price ?? opts.price);
  const expirySel = select(
    "Expiration",
    [
      { value: "never", label: "Never" },
      { value: "30d", label: "In 30 days" },
      ...(editing && alert.expires_at !== null ? [{ value: "keep", label: "Keep the current date" }] : []),
    ],
    editing ? (alert.expires_at === null ? "never" : "keep") : "30d",
  );
  const nameInput = h("input", { class: "input", maxlength: "200", spellcheck: "false", "aria-label": "Message", placeholder: "Named from the condition when left empty" });
  nameInput.value = alert?.name ?? "";

  const field = (name: string, label: string, ...content: HTMLElement[]) =>
    h("div", { class: "al-row", "data-field": name }, h("span", { class: "al-label" }, label), h("div", { class: "al-value" }, ...content));

  const rows = {
    direction: field("direction", "Direction", directionSel),
    value: field("value", "Number", numberInput),
    other: field("other", "Crosses", otherSel),
  };
  const syncFields = () => {
    rows.direction.hidden = condition === "signal";
    rows.value.hidden = condition !== "cross_value";
    rows.other.hidden = condition !== "cross_output";
  };
  conditionSel.addEventListener("change", () => {
    condition = conditionSel.value as Condition;
    refillOutputs(outputSel.value);
    refillOther();
    syncFields();
  });
  outputSel.addEventListener("change", refillOther);
  syncFields();

  const check = (channel: AlertChannel, label: string, hint?: string) => {
    const box = h("input", { type: "checkbox" });
    box.checked = notify.has(channel);
    box.addEventListener("change", () => (box.checked ? notify.add(channel) : notify.delete(channel)));
    return h("label", { class: "check" }, box, h("span", {}, label), hint ? h("small", {}, hint) : null);
  };

  const warnings = h("ul", { class: "al-warnings", hidden: true });
  const showWarnings = (codes: string[]) => {
    warnings.replaceChildren(...codes.map((w) => h("li", {}, w)));
    warnings.hidden = codes.length === 0;
  };
  const known = alert ? viewOf(alert) : null;
  showWarnings(known?.warnings ?? []);

  const result = resultLine();
  const save = h("button", { class: "btn btn-light", type: "button" }, editing ? "Save" : "Create");
  const cancel = h("button", { class: "btn btn-secondary", type: "button" }, "Cancel");
  const remove = editing ? button({ icon: "trash", title: "Delete this alert", class: "danger-icon", onClick: () => confirmDelete() }) : null;
  const note =
    editing && known && (known.kind === "stopped" || known.kind === "error")
      ? h("div", { class: "al-status-note" }, known.kind === "stopped" ? `This alert is stopped (${known.reason}). Saving it restarts it.` : `This alert has an error: ${known.reason}. Saving it checks it again.`)
      : null;

  const body = h(
    "div",
    { class: "al-body" },
    note,
    h("div", { class: "al-indicator", "data-tip": def.title }, opts.title),
    field("condition", "Condition", conditionSel),
    field("output", "Output", outputSel),
    rows.direction,
    rows.value,
    rows.other,
    h("p", { class: "al-closed-note" }, closedBarNote(timeframe)),
    warnings,
    h("hr"),
    field("frequency", "Trigger", freqSel),
    field("expiry", "Expiration", expirySel),
    field("name", "Message", nameInput),
    field("notify", "Notifications", h("div", { class: "checks" }, check("telegram", "Telegram", opts.telegramReady ? undefined : "not set up yet (Settings)"), check("app", "Toast and sound in open browsers"))),
    field("price", "Price", priceSel),
    result.el,
  );
  // A complaint about an earlier try goes away as soon as the form changes.
  for (const type of ["input", "change"]) body.addEventListener(type, () => result.clear());
  const foot = h("div", { class: "al-foot" }, remove, h("div", { class: "spacer" }), cancel, save);
  const close = button({ icon: "x", title: "Close (Esc)", onClick: () => done() });
  const modal = h(
    "div",
    { class: "modal alert-dialog indicator-alert-dialog", role: "dialog", "aria-modal": "true", "aria-label": editing ? "Edit alert" : "Create alert" },
    h("div", { class: "modal-head" }, h("h2", {}, editing ? "Edit alert on" : "Create alert on", h("span", { class: "al-chip" }, `${opts.instrument}, ${timeframe}`)), close),
    body,
    foot,
  );
  const backdrop = h("div", { class: "modal-backdrop" }, modal);
  const onKey = (ev: KeyboardEvent) => {
    if (ev.key === "Escape" && !document.querySelector(".menu, .confirm-backdrop")) {
      ev.stopPropagation();
      done();
    } else if (ev.key === "Enter" && (ev.target as HTMLElement).tagName === "INPUT" && (ev.target as HTMLInputElement).type === "text") {
      ev.preventDefault();
      void submit();
    }
  };
  function done(): void {
    backdrop.remove();
    document.removeEventListener("keydown", onKey, true);
  }
  backdrop.addEventListener("pointerdown", (ev) => {
    if (ev.target === backdrop) done();
  });
  cancel.addEventListener("click", done);
  save.addEventListener("click", () => void submit());
  document.addEventListener("keydown", onKey, true);
  document.body.append(backdrop);
  (condition === "cross_value" ? numberInput : save).focus();

  const form = (): IndicatorForm => ({
    output: outputSel.value,
    condition,
    direction: directionSel.value as AlertDirection,
    value: numberInput.value,
    otherOutput: otherSel.value,
  });

  async function submit(): Promise<void> {
    const f = form();
    const problem = validateForm(f, def);
    if (problem) {
      result.fail(problem);
      return;
    }
    const { direction, indicator } = buildIndicatorInput(opts.uid, f);
    const frequency = freqSel.value as AlertFrequency;
    const input: AlertInput = {
      direction,
      trigger_price: priceSel.value as Price,
      trigger_mode: "candle_close",
      timeframe,
      frequency,
      cooldown_s: 0,
      notify: [...notify],
      indicator,
    };
    const name = nameToSend({
      typed: nameInput.value,
      original: alert,
      instrument: opts.instrument,
      timeframe,
      direction,
      next: { name: spec?.name ?? opts.title, output: indicator.output, condition: indicator.condition, other_output: indicator.other_output ?? null, value: indicator.value ?? null },
    });
    if (name) input.name = name;
    if (expirySel.value === "30d") input.expires_at = Math.floor(Date.now() / 1000) + 30 * DAY;
    else if (expirySel.value === "never" && editing) input.clear_expiry = true;
    save.disabled = true;
    result.busy(editing ? "Saving…" : "Creating…");
    try {
      const saved = editing ? await alertsApi.update(alert.id, { ...input, status: "active" }) : await alertsApi.create({ ...input, instrument: opts.instrument, type: "indicator" });
      opts.onSaved?.(saved);
      const codes = saved.state?.warnings ?? [];
      if (codes.length) finished(codes);
      else done();
    } catch (err) {
      result.fail(problemText(err));
    } finally {
      save.disabled = false;
    }
  }

  /** Created with warnings: stay open so they are read. They never block. */
  function finished(codes: string[]): void {
    const doneBtn = h("button", { class: "btn btn-light", type: "button" }, "Done");
    doneBtn.addEventListener("click", done);
    body.replaceChildren(
      h("p", { class: "al-created" }, `The alert is ${editing ? "saved" : "created"}. It records where the indicator is on the next closed ${timeframe} bar and can fire from the bar after.`),
      h("p", { class: "al-note" }, "Things to know about this indicator (the alert still runs):"),
      h("ul", { class: "al-warnings" }, ...codes.map((w) => h("li", {}, warningText(w)))),
    );
    foot.replaceChildren(h("div", { class: "spacer" }), doneBtn);
    doneBtn.focus();
  }

  function confirmDelete(): void {
    confirmAlertDelete(alert!, () => {
      opts.onDeleted?.(alert!);
      done();
    });
  }
}
