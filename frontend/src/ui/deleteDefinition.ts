/**
 * The dialog for deleting one of your indicator definitions: asks where it is used first,
 * lists that when it is, and only then deletes. The wording and the meaning of each answer
 * are in `definitionDelete.ts`; this draws them and makes the calls.
 */

import { customApi } from "../api";
import { afterDelete, afterError, afterUsage, confirmLabel, usageLines, type DeleteNext } from "./definitionDelete";
import { button, h } from "./dom";
import { resultLine } from "./forms";
import { toast } from "./toast";

export function openDeleteDefinition(opts: {
  id: string;
  title: string;
  /** The catalog, the charts or the layouts may have changed: read them again. */
  onChanged: () => void;
}): void {
  const result = resultLine();
  const text = h("p", { class: "confirm-text" }, "Checking where it is used…");
  const usage = h("div", { class: "ind-usage" });
  const cancel = h("button", { class: "btn btn-secondary", type: "button" }, "Cancel");
  const del = h("button", { class: "btn btn-danger", type: "button", hidden: true }, "Delete");
  const box = h(
    "div",
    { class: "modal confirm-dialog ind-delete", role: "alertdialog", "aria-modal": "true", "aria-label": `Delete ${opts.title}` },
    h("div", { class: "modal-head" }, h("h2", {}, "Delete indicator"), button({ icon: "x", title: "Close", onClick: () => close() })),
    text,
    usage,
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

  let inUse = false;
  const lists = (next: DeleteNext) => {
    usage.replaceChildren();
    if (next.phase !== "confirm" || !next.usage) return;
    const { charts, layouts } = usageLines(next.usage);
    for (const [heading, lines] of [["Charts", charts], ["Saved layouts", layouts]] as const) {
      if (lines.length) usage.append(h("div", {}, h("b", {}, heading), h("ul", {}, ...lines.map((l) => h("li", {}, l)))));
    }
  };
  const show = (next: DeleteNext) => {
    if (next.refresh) opts.onChanged();
    text.textContent = next.text;
    lists(next);
    result.clear();
    if (next.phase === "confirm") {
      inUse = next.inUse === true;
      del.textContent = confirmLabel(inUse);
      del.hidden = false;
      del.disabled = false;
      cancel.textContent = "Cancel";
      del.focus();
    } else {
      del.hidden = true;
      cancel.textContent = "Close";
      cancel.focus();
    }
  };

  cancel.addEventListener("click", close);
  del.addEventListener("click", async () => {
    del.disabled = true;
    result.busy("Deleting…");
    try {
      const done = await customApi.remove(opts.id, inUse);
      close();
      toast({ ...afterDelete(opts.title, done), icon: "trash" });
      opts.onChanged();
    } catch (err) {
      show(afterError(opts.title, err));
    }
  });

  document.addEventListener("keydown", onKey, true);
  document.body.append(backdrop);
  customApi
    .usage(opts.id)
    .then((u) => show(afterUsage(opts.title, u)))
    .catch((err) => show(afterError(opts.title, err)));
}
