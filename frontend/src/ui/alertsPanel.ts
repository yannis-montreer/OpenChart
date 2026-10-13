/** Alerts panel in the right column: the alerts (status, pause, edit, delete) and a log. */

import { alertsApi, type Alert, type AlertEvent } from "../api";
import { confirmAlertDelete } from "./alertDialog";
import { button, h, icon } from "./dom";

const TRIGGER_TEXT = (a: Alert) =>
  a.trigger_mode === "candle_close"
    ? "Once per bar close"
    : ({ once: "Once only", once_per_bar: "Once per bar", repeating: "Once per minute" } as const)[a.frequency];
const DIRECTION_TEXT = { cross: "Crossing", up: "Crossing Up", down: "Crossing Down" } as const;
const STATUS_TEXT = { active: "Active", triggered: "Triggered", expired: "Expired", stopped: "Stopped" } as const;
const LATE_TEXT = { wake: "after wake-up", reconnect: "after a feed outage", restart: "after a restart" } as const;

function when(ts: number): string {
  const d = new Date(ts * 1000);
  const today = new Date().toDateString() === d.toDateString();
  const time = { hour: "2-digit", minute: "2-digit", hourCycle: "h23" } as const;
  return d.toLocaleString("en-US", today ? { ...time, second: "2-digit" } : { ...time, month: "short", day: "numeric" });
}

export class AlertsPanel {
  readonly el: HTMLElement;
  private alerts = new Map<string, Alert>();
  private events: AlertEvent[] = [];
  private tab: "alerts" | "log" = "alerts";
  private body: HTMLElement;
  private tabs: HTMLElement;
  /** Open the edit dialog for an alert. */
  onEdit: (alert: Alert) => void = () => {};
  /** Decimals for prices of an instrument. */
  decimals: (instrument: string) => number = () => 5;

  constructor() {
    this.tabs = h("div", { class: "seg al-tabs", role: "tablist" });
    this.body = h("div", { class: "al-list", role: "list" });
    this.el = h(
      "div",
      { class: "alerts-panel" },
      h("div", { class: "panel-head" }, h("span", { class: "title" }, "Alerts"), this.tabs),
      this.body,
    );
    this.renderTabs();
  }

  async load(): Promise<void> {
    try {
      const [alerts, events] = await Promise.all([alertsApi.list(), alertsApi.events(200)]);
      this.alerts = new Map(alerts.map((a) => [a.id, a]));
      this.events = events;
    } catch (err) {
      this.body.replaceChildren(h("div", { class: "panel-empty" }, `Could not load alerts: ${(err as Error).message}`));
      return;
    }
    this.render();
  }

  /** Alerts on a drawing (to offer editing them). */
  forDrawing(drawingId: string): Alert[] {
    return [...this.alerts.values()].filter((a) => a.drawing_id === drawingId);
  }

  changed(op: "create" | "update" | "delete", alert: Alert): void {
    if (op === "delete") this.alerts.delete(alert.id);
    else this.alerts.set(alert.id, alert);
    this.render();
  }

  triggered(alert: Alert, event: AlertEvent): void {
    this.alerts.set(alert.id, alert);
    this.events = [event, ...this.events.filter((e) => e.id !== event.id)].slice(0, 500);
    this.render();
  }

  eventChanged(event: AlertEvent): void {
    this.events = this.events.map((e) => (e.id === event.id ? event : e));
    if (this.tab === "log") this.render();
  }

  private renderTabs(): void {
    const tab = (id: "alerts" | "log", label: string) => {
      const b = h("button", { type: "button", class: `btn${this.tab === id ? " active" : ""}`, role: "tab" }, label);
      b.addEventListener("click", () => {
        this.tab = id;
        this.renderTabs();
        this.render();
      });
      return b;
    };
    this.tabs.replaceChildren(tab("alerts", "Alerts"), tab("log", "Log"));
  }

  private render(): void {
    if (this.tab === "alerts") this.renderAlerts();
    else this.renderLog();
  }

  private renderAlerts(): void {
    const alerts = [...this.alerts.values()].sort((a, b) => b.created_at - a.created_at);
    if (!alerts.length) {
      this.body.replaceChildren(
        h("div", { class: "panel-empty" }, "No alerts yet. Right-click a line and choose Add alert, or use the Alert button for a price level (Alt+A)."),
      );
      return;
    }
    this.body.replaceChildren(...alerts.map((a) => this.alertRow(a)));
  }

  private alertRow(a: Alert): HTMLElement {
    const active = a.status === "active";
    const target = a.type === "level_cross" ? `${a.level}` : "line";
    const facts = [
      `${DIRECTION_TEXT[a.direction]} ${target}`,
      TRIGGER_TEXT(a),
      a.timeframe,
    ].join(" · ");
    const last = a.last_fired_at
      ? `Fired ${a.fire_count}×, last ${when(a.last_fired_at)}`
      : a.expires_at
        ? `Expires ${when(a.expires_at)}`
        : "Never expires";
    const toggle = button({
      icon: active ? "pause" : "play",
      title: active ? "Stop this alert" : "Restart this alert",
      onClick: async (ev) => {
        ev.stopPropagation();
        try {
          this.changed("update", await alertsApi.update(a.id, { status: active ? "stopped" : "active" }));
        } catch (err) {
          window.alert((err as Error).message);
        }
      },
    });
    const edit = button({
      icon: "pencil",
      title: "Edit",
      onClick: (ev) => {
        ev.stopPropagation();
        this.onEdit(a);
      },
    });
    const del = button({
      icon: "trash",
      title: "Delete",
      onClick: (ev) => {
        ev.stopPropagation();
        confirmAlertDelete(a, () => this.changed("delete", a));
      },
    });
    const row = h(
      "div",
      { class: `al-item status-${a.status}`, role: "listitem", tabindex: "0" },
      h("span", { class: "al-dot", "data-tip": STATUS_TEXT[a.status] }),
      h(
        "div",
        { class: "al-text" },
        h("div", { class: "al-name" }, a.name),
        h("div", { class: "al-facts" }, `${a.instrument} · ${facts}`),
        h("div", { class: "al-facts" }, active ? last : `${STATUS_TEXT[a.status]}. ${last}`),
      ),
      h("div", { class: "al-actions" }, toggle, edit, del),
    );
    row.addEventListener("dblclick", () => this.onEdit(a));
    return row;
  }

  private renderLog(): void {
    if (!this.events.length) {
      this.body.replaceChildren(h("div", { class: "panel-empty" }, "Nothing has fired yet."));
      return;
    }
    this.body.replaceChildren(
      ...this.events.map((e) => {
        const alert = this.alerts.get(e.alert_id);
        const d = this.decimals(alert?.instrument ?? "");
        const fmt = (x: number | null) => (x === null ? "–" : x.toLocaleString("en-US", { minimumFractionDigits: d, maximumFractionDigits: d }));
        const delivery = e.delivered
          ? h("span", { class: "al-badge ok", "data-tip": e.attempts ? `Sent to Telegram${e.attempts > 1 ? ` after ${e.attempts} attempts` : ""}` : "Shown in the app" }, icon("check", 14))
          : e.error
            ? h("span", { class: "al-badge bad", "data-tip": `Not delivered: ${e.error}` }, icon("warn", 14))
            : h("span", { class: "al-badge", "data-tip": "Sending…" }, icon("clock", 14));
        return h(
          "div",
          { class: "al-event", role: "listitem" },
          h(
            "div",
            { class: "al-event-head" },
            h("span", { class: "al-time num" }, when(e.ts)),
            h("span", { class: `al-cross ${e.cross}` }, icon(e.cross === "up" ? "crossUp" : "crossDown", 14)),
            delivery,
          ),
          h("div", { class: "al-name" }, alert?.name ?? e.alert_id),
          h("div", { class: "al-facts num" }, `${fmt(e.price)} · line ${fmt(e.line_value)}`),
          e.late
            ? h("div", { class: "al-late" }, icon("history", 14), `Detected ${LATE_TEXT[e.late]}, at ${when(e.detected_at)}`)
            : null,
        );
      }),
    );
  }
}
