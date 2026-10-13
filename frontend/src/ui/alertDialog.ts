/**
 * Create / edit alert dialog (SPEC section 9), laid out like the owner's reference:
 * condition (Price, crossing / up / down, the line or a level), trigger, expiration,
 * message and notifications, then Cancel and Create (or a delete button and Save).
 */

import {
  ApiError,
  alertsApi,
  type Alert,
  type AlertChannel,
  type AlertDirection,
  type AlertFrequency,
  type AlertInput,
  type AlertMode,
  type Drawing,
  type Price,
} from "../api";
import { button, h, icon, menuItem, showMenu, type IconName } from "./dom";
import { resultLine } from "./forms";

/** Trigger choices, as in the reference: each maps to the spec's options. */
type Trigger = "once" | "per_bar" | "bar_close" | "per_minute";

const TRIGGERS: { id: Trigger; label: string; hint: string }[] = [
  { id: "once", label: "Once only", hint: "Triggers once when the condition is met, then stops" },
  { id: "per_bar", label: "Once per bar", hint: "Triggers at most once per bar when the condition is met" },
  { id: "bar_close", label: "Once per bar close", hint: "Checks the bar's close against the line when each bar closes" },
  { id: "per_minute", label: "Once per minute", hint: "Triggers every time, at most once a minute" },
];

const DIRECTIONS: { id: AlertDirection; label: string; icon: IconName }[] = [
  { id: "cross", label: "Crossing", icon: "crossing" },
  { id: "up", label: "Crossing Up", icon: "crossUp" },
  { id: "down", label: "Crossing Down", icon: "crossDown" },
];

const LINE_NAMES: Record<string, string> = {
  hline: "Horizontal line",
  trendline: "Trend line",
  ray: "Ray",
  extended: "Extended line",
};

const DAY = 86400;

export function triggerOf(a: Pick<Alert, "frequency" | "trigger_mode">): Trigger {
  if (a.trigger_mode === "candle_close") return "bar_close";
  return ({ once: "once", once_per_bar: "per_bar", repeating: "per_minute" } as Record<AlertFrequency, Trigger>)[a.frequency];
}

export function triggerFields(t: Trigger): { frequency: AlertFrequency; trigger_mode: AlertMode; cooldown_s: number } {
  switch (t) {
    case "once":
      return { frequency: "once", trigger_mode: "touch", cooldown_s: 0 };
    case "per_bar":
      return { frequency: "once_per_bar", trigger_mode: "touch", cooldown_s: 0 };
    case "bar_close":
      return { frequency: "repeating", trigger_mode: "candle_close", cooldown_s: 0 };
    case "per_minute":
      return { frequency: "repeating", trigger_mode: "touch", cooldown_s: 60 };
  }
}

/** "2026-11-11T19:42" in local time, for <input type=datetime-local>. */
function localInput(ts: number): string {
  const d = new Date(ts * 1000);
  const pad = (n: number) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

function expiryText(ts: number | null): string {
  if (ts === null) return "Never";
  // "November 11, 2026 at 19:42", like the rest of the (English) interface.
  return new Date(ts * 1000).toLocaleString("en-US", {
    month: "long",
    day: "numeric",
    year: "numeric",
    hour: "2-digit",
    minute: "2-digit",
    hourCycle: "h23",
  });
}

export interface AlertTarget {
  instrument: string;
  timeframe: string; // the chart's, for "once per bar" and "bar close"
  price: Price; // the price the chart shows
  decimals: number;
  drawing?: Drawing; // a line alert
  level?: number; // a level alert (from the current price)
}

export interface AlertDialogOptions {
  target: AlertTarget;
  alert?: Alert; // edit this one
  telegramReady: boolean;
  onSaved?: (alert: Alert) => void;
  onDeleted?: (alert: Alert) => void;
}

export function openAlertDialog(opts: AlertDialogOptions): void {
  const { target, alert } = opts;
  const editing = alert !== undefined;
  const isLine = editing ? alert.type === "line_cross" : target.drawing !== undefined;
  const timeframe = editing ? alert.timeframe : target.timeframe.endsWith("s") ? "1m" : target.timeframe;
  const lineName = target.drawing ? LINE_NAMES[target.drawing.type] : "Line";

  let direction: AlertDirection = alert?.direction ?? "cross";
  let trigger: Trigger = alert ? triggerOf(alert) : "once";
  let expires: number | null = alert ? alert.expires_at : Math.floor(Date.now() / 1000) + 30 * DAY;
  let price: Price = alert?.trigger_price ?? target.price;
  const notify = new Set<AlertChannel>(alert?.notify ?? ["telegram", "app"]);

  const defaultName = () => {
    const what = DIRECTIONS.find((d) => d.id === direction)!.label;
    const on = isLine ? lineName.toLowerCase() : levelInput.value || "level";
    return `${target.instrument}, ${timeframe} ${what} ${on}`;
  };
  let nameTouched = editing;

  // Condition
  const source = h("select", { class: "input", disabled: true, "data-tip": "Indicator alerts come with indicators (M7)" }, h("option", {}, "Price"));
  const dirBtn = h("button", { class: "input select-btn", type: "button" });
  const renderDir = () => {
    const d = DIRECTIONS.find((x) => x.id === direction)!;
    dirBtn.replaceChildren(icon(d.icon, 16), h("span", {}, d.label), icon("chevron", 14));
  };
  dirBtn.addEventListener("click", () =>
    showMenu(
      dirBtn,
      h(
        "div",
        { class: "select-menu", style: `min-width:${dirBtn.offsetWidth}px` },
        ...DIRECTIONS.map((d) =>
          menuItem({
            label: d.label,
            icon: d.icon,
            selected: d.id === direction,
            onClick: () => {
              direction = d.id;
              renderDir();
              if (!nameTouched) nameInput.value = defaultName();
            },
          }),
        ),
      ),
    ),
  );
  renderDir();
  const levelInput = h("input", { class: "input num", type: "number", step: "any", inputmode: "decimal" });
  levelInput.value = String(alert?.level ?? (target.level !== undefined ? target.level.toFixed(target.decimals) : ""));
  levelInput.addEventListener("input", () => {
    if (!nameTouched) nameInput.value = defaultName();
  });
  const targetEl = isLine
    ? h("div", { class: "input readonly", "data-tip": "The line this alert watches" }, lineName)
    : levelInput;

  // Trigger
  const triggerBtn = h("button", { class: "link-btn", type: "button" });
  const renderTrigger = () => triggerBtn.replaceChildren(h("span", {}, TRIGGERS.find((t) => t.id === trigger)!.label), icon("chevron", 14));
  triggerBtn.addEventListener("click", () =>
    showMenu(
      triggerBtn,
      h(
        "div",
        { class: "select-menu trigger-menu" },
        ...TRIGGERS.map((t) => {
          const item = menuItem({
            label: t.label,
            selected: t.id === trigger,
            onClick: () => {
              trigger = t.id;
              renderTrigger();
            },
          });
          item.append(h("span", { class: "menu-sub" }, t.hint));
          return item;
        }),
      ),
    ),
  );
  renderTrigger();

  // Expiration
  const expiryBtn = h("button", { class: "link-btn", type: "button" });
  const renderExpiry = () => expiryBtn.replaceChildren(h("span", {}, expiryText(expires)), icon("chevron", 14));
  expiryBtn.addEventListener("click", () => {
    const input = h("input", { class: "input", type: "datetime-local" });
    input.value = localInput(expires ?? Math.floor(Date.now() / 1000) + 30 * DAY);
    const set = () => {
      const t = new Date(input.value).getTime() / 1000;
      if (Number.isFinite(t)) {
        expires = Math.floor(t);
        renderExpiry();
      }
    };
    input.addEventListener("change", set);
    showMenu(
      expiryBtn,
      h(
        "div",
        { class: "expiry-menu" },
        input,
        menuItem({ label: "Never expires", selected: expires === null, onClick: () => ((expires = null), renderExpiry()) }),
        menuItem({ label: "In 30 days", onClick: () => ((expires = Math.floor(Date.now() / 1000) + 30 * DAY), renderExpiry()) }),
      ),
    );
  });
  renderExpiry();

  // Message
  const nameInput = h("input", { class: "input", maxlength: "200", spellcheck: "false" });
  nameInput.value = alert?.name ?? defaultName();
  nameInput.addEventListener("input", () => (nameTouched = true));

  // Notifications
  const check = (channel: AlertChannel, label: string, hint?: string) => {
    const box = h("input", { type: "checkbox" });
    box.checked = notify.has(channel);
    box.addEventListener("change", () => (box.checked ? notify.add(channel) : notify.delete(channel)));
    return h("label", { class: "check" }, box, h("span", {}, label), hint ? h("small", {}, hint) : null);
  };
  const channels = h(
    "div",
    { class: "checks" },
    check("telegram", "Telegram", opts.telegramReady ? undefined : "not set up yet (Settings)"),
    check("app", "Toast and sound in open browsers"),
  );

  // More: which price
  const priceSeg = h("div", { class: "seg" });
  const renderPrice = () =>
    priceSeg.replaceChildren(
      ...(["bid", "mid", "ask"] as Price[]).map((p) => {
        const b = h("button", { type: "button", class: `btn${p === price ? " active" : ""}` }, p[0].toUpperCase() + p.slice(1));
        b.addEventListener("click", () => {
          price = p;
          renderPrice();
        });
        return b;
      }),
    );
  renderPrice();

  const row = (label: string, ...content: HTMLElement[]) =>
    h("div", { class: "al-row" }, h("span", { class: "al-label" }, label), h("div", { class: "al-value" }, ...content));

  const result = resultLine();
  const save = h("button", { class: "btn btn-light", type: "button" }, editing ? "Save" : "Create");
  const cancel = h("button", { class: "btn btn-secondary", type: "button" }, "Cancel");
  const remove = editing
    ? button({ icon: "trash", title: "Delete this alert", class: "danger-icon", onClick: () => confirmDelete() })
    : null;

  const chip = h("span", { class: "al-chip" }, `${target.instrument}, ${timeframe}`);
  const close = button({ icon: "x", title: "Close (Esc)", onClick: () => done() });
  const modal = h(
    "div",
    { class: "modal alert-dialog", role: "dialog", "aria-modal": "true", "aria-label": editing ? "Edit alert" : "Create alert" },
    h("div", { class: "modal-head" }, h("h2", {}, editing ? "Edit alert on" : "Create alert on", chip), close),
    h(
      "div",
      { class: "al-body" },
      row("Condition", source, dirBtn, targetEl),
      h("hr"),
      row("Trigger", triggerBtn),
      row("Expiration", expiryBtn),
      row("Message", nameInput),
      row("Notifications", channels),
      row("Price", priceSeg, h("small", { class: "al-note" }, "The price checked against the line")),
      result.el,
    ),
    h("div", { class: "al-foot" }, remove, h("div", { class: "spacer" }), cancel, save),
  );
  const backdrop = h("div", { class: "modal-backdrop" }, modal);
  const onKey = (ev: KeyboardEvent) => {
    if (ev.key === "Escape" && !document.querySelector(".menu")) {
      ev.stopPropagation();
      done();
    } else if (ev.key === "Enter" && (ev.target as HTMLElement).tagName === "INPUT") {
      ev.preventDefault();
      void submit();
    }
  };
  const done = () => {
    backdrop.remove();
    document.removeEventListener("keydown", onKey, true);
  };
  backdrop.addEventListener("pointerdown", (ev) => {
    if (ev.target === backdrop) done();
  });
  cancel.addEventListener("click", done);
  save.addEventListener("click", () => void submit());
  document.addEventListener("keydown", onKey, true);
  document.body.append(backdrop);
  (isLine ? save : levelInput).focus();

  async function submit(): Promise<void> {
    const body: AlertInput = {
      direction,
      trigger_price: price,
      timeframe,
      ...triggerFields(trigger),
      name: nameInput.value.trim() || defaultName(),
      notify: [...notify],
    };
    if (expires === null) body.clear_expiry = true;
    else body.expires_at = expires;
    if (!isLine) {
      const level = Number(levelInput.value);
      if (!levelInput.value || !Number.isFinite(level)) {
        result.fail("Enter the price level.");
        levelInput.focus();
        return;
      }
      body.level = level;
    }
    save.disabled = true;
    result.busy(editing ? "Saving…" : "Creating…");
    try {
      let saved: Alert;
      if (editing) {
        saved = await alertsApi.update(alert.id, { ...body, status: "active" });
      } else {
        const { clear_expiry: _ignored, ...create } = body;
        saved = await alertsApi.create({
          ...create,
          instrument: target.instrument,
          type: isLine ? "line_cross" : "level_cross",
          drawing_id: target.drawing?.id,
        });
      }
      opts.onSaved?.(saved);
      done();
    } catch (err) {
      result.fail(err instanceof ApiError ? err.message : String(err));
    } finally {
      save.disabled = false;
    }
  }

  function confirmDelete(): void {
    confirmAlertDelete(alert!, () => {
      opts.onDeleted?.(alert!);
      done();
    });
  }
}

/** "Delete this alert?" with Cancel and a red Delete, as in the reference. */
export function confirmAlertDelete(alert: Alert, onDeleted: () => void): void {
  const result = resultLine();
  const del = h("button", { class: "btn btn-danger", type: "button" }, "Delete");
  const cancel = h("button", { class: "btn btn-secondary", type: "button" }, "Cancel");
  const box = h(
    "div",
    { class: "modal confirm-dialog", role: "alertdialog", "aria-modal": "true", "aria-label": "Delete this alert?" },
    h("div", { class: "modal-head" }, h("h2", {}, "Delete this alert?"), button({ icon: "x", title: "Close", onClick: () => close() })),
    h("p", { class: "confirm-text" }, `This will permanently delete your "${alert.name}" alert.`),
    result.el,
    h("div", { class: "al-foot" }, h("div", { class: "spacer" }), cancel, del),
  );
  const backdrop = h("div", { class: "modal-backdrop confirm-backdrop" }, box);
  const onKey = (ev: KeyboardEvent) => {
    if (ev.key === "Escape") {
      ev.stopPropagation();
      close();
    }
  };
  const close = () => {
    backdrop.remove();
    document.removeEventListener("keydown", onKey, true);
  };
  cancel.addEventListener("click", close);
  del.addEventListener("click", async () => {
    del.disabled = true;
    try {
      await alertsApi.remove(alert.id);
      close();
      onDeleted();
    } catch (err) {
      result.fail(err instanceof ApiError ? err.message : String(err));
      del.disabled = false;
    }
  });
  document.addEventListener("keydown", onKey, true);
  document.body.append(backdrop);
  del.focus();
}
