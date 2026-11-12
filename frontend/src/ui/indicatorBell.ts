/** The bell on an indicator's legend row: opens the alert dialog, and shows how many alerts the instance has. */

import type { Alert } from "../api";
import { bellTitle } from "../alerts/indicatorAlerts";
import { button, h } from "./dom";

export function bellButton(alerts: readonly Pick<Alert, "status">[], onClick: (bell: HTMLButtonElement) => void): HTMLButtonElement {
  const active = alerts.some((a) => a.status === "active");
  const b = button({
    icon: active ? "bellRing" : alerts.length ? "bellOff" : "bell",
    title: bellTitle(alerts),
    class: `legend-bell${alerts.length ? (active ? " alert-on" : " alert-off") : ""}`,
    onClick: (ev) => {
      ev.stopPropagation();
      onClick(b);
    },
  });
  if (alerts.length) b.append(h("span", { class: "bell-count" }, String(alerts.length)));
  return b;
}
