/**
 * A yes/no question that lists the indicator alerts a step would stop. Resolves true on the
 * confirming button, false on anything else (Cancel, Escape, the backdrop). The button that is
 * focused is Cancel: stopping alerts is never one Enter away.
 */

import { button, h } from "./dom";

export function confirmWithAlerts(opts: { title: string; text: string; lines: string[]; confirm: string; danger?: boolean }): Promise<boolean> {
  return new Promise((resolve) => {
    const ok = h("button", { class: `btn ${opts.danger ? "btn-danger" : "btn-light"}`, type: "button" }, opts.confirm);
    const no = h("button", { class: "btn btn-secondary", type: "button" }, "Cancel");
    const box = h(
      "div",
      { class: "modal confirm-dialog", role: "alertdialog", "aria-modal": "true", "aria-label": opts.title },
      h("div", { class: "modal-head" }, h("h2", {}, opts.title), button({ icon: "x", title: "Close", onClick: () => done(false) })),
      h("p", { class: "confirm-text" }, opts.text),
      opts.lines.length ? h("ul", { class: "confirm-list" }, ...opts.lines.map((l) => h("li", {}, l))) : null,
      h("div", { class: "al-foot" }, h("div", { class: "spacer" }), no, ok),
    );
    const backdrop = h("div", { class: "modal-backdrop confirm-backdrop" }, box);
    const onKey = (ev: KeyboardEvent) => {
      if (ev.key === "Escape") {
        ev.stopPropagation();
        done(false);
      }
    };
    const done = (answer: boolean) => {
      backdrop.remove();
      document.removeEventListener("keydown", onKey, true);
      resolve(answer);
    };
    ok.addEventListener("click", () => done(true));
    no.addEventListener("click", () => done(false));
    backdrop.addEventListener("pointerdown", (ev) => {
      if (ev.target === backdrop) done(false);
    });
    document.addEventListener("keydown", onKey, true);
    document.body.append(backdrop);
    no.focus();
  });
}
