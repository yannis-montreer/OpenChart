/** A yes/no question over everything else. Resolves true on the confirming button, false on anything else (Cancel, Escape, the backdrop). */

import { button, h } from "./dom";

export function confirmDialog(opts: { title: string; text: string; confirm: string; cancel?: string; danger?: boolean }): Promise<boolean> {
  return new Promise((resolve) => {
    const ok = h("button", { class: `btn ${opts.danger ? "btn-danger" : "btn-light"}`, type: "button" }, opts.confirm);
    const no = h("button", { class: "btn btn-secondary", type: "button" }, opts.cancel ?? "Cancel");
    const box = h(
      "div",
      { class: "modal confirm-dialog", role: "alertdialog", "aria-modal": "true", "aria-label": opts.title },
      h("div", { class: "modal-head" }, h("h2", {}, opts.title), button({ icon: "x", title: "Close", onClick: () => done(false) })),
      h("p", { class: "confirm-text" }, opts.text),
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
    (opts.danger ? no : ok).focus();
  });
}
