/**
 * Settings, Removed, "Delete data…": a confirmation over the settings dialog that lists what
 * goes and its size, keeps the drawings and alerts unless their box is ticked, and asks for
 * the instrument's name. The texts and rules are in `instruments.ts`; the server refuses a
 * delete whose preview is stale (409), and the dialog then shows the fresh preview.
 */

import { account, ApiError, type DeletedInstrument, type DeletePreview } from "../api";
import { canDelete, deletedText, deleteLines, deleteSize, drawingsAndAlerts } from "../instruments";
import { button, h } from "./dom";
import { field, resultLine, textInput } from "./forms";

const message = (err: unknown) => (err instanceof Error ? err.message : String(err));

/** Resolves with the server's answer once deleted, or null when closed without deleting. */
export function deleteInstrumentDialog(name: string): Promise<{ result: DeletedInstrument; text: string } | null> {
  return new Promise((resolve) => {
    let preview: DeletePreview | undefined;
    let working = false;
    const intro = h("p", { class: "confirm-text" }, `Measuring what is stored for ${name}…`);
    const list = h("ul", { class: "delete-list" });
    const both = h("input", { type: "checkbox" });
    const bothLabel = h("span", {});
    const bothRow = h("label", { class: "check delete-both", hidden: true }, both, bothLabel);
    const typed = textInput({ placeholder: name });
    typed.setAttribute("aria-label", `Type ${name} to confirm`);
    const result = resultLine();
    const ok = h("button", { class: "btn btn-danger", type: "button", disabled: true }, "Delete");
    const no = h("button", { class: "btn btn-secondary", type: "button" }, "Cancel");
    const box = h(
      "div",
      { class: "modal confirm-dialog delete-dialog", role: "alertdialog", "aria-modal": "true", "aria-label": `Delete ${name}` },
      h("div", { class: "modal-head" }, h("h2", {}, `Delete ${name}`), button({ icon: "x", title: "Close", onClick: () => done(null) })),
      intro,
      h("div", { class: "delete-body" }, list, bothRow, field(`Type ${name} to confirm`, typed)),
      result.el,
      h("div", { class: "al-foot" }, h("div", { class: "spacer" }), no, ok),
    );
    const backdrop = h("div", { class: "modal-backdrop confirm-backdrop" }, box);

    const render = () => {
      if (!preview) return;
      const s = preview.stored;
      intro.textContent = `Delete everything stored for ${name}, ${deleteSize(preview)}? This cannot be undone. Adding it back later downloads its history afresh.`;
      list.replaceChildren(...deleteLines(preview, both.checked).map((line) => h("li", {}, line)));
      bothRow.hidden = !preview.needs_drawings_and_alerts;
      bothLabel.textContent = `Also delete its ${drawingsAndAlerts(s.drawings, s.alerts)}`;
      ok.disabled = working || !canDelete(preview, typed.value, both.checked);
    };
    const load = async () => {
      try {
        preview = await account.deletePreview(name);
        render();
      } catch (err) {
        preview = undefined;
        intro.textContent = `${name} cannot be deleted now.`;
        list.replaceChildren();
        ok.disabled = true;
        result.fail(message(err));
      }
    };

    const onKey = (ev: KeyboardEvent) => {
      if (ev.key === "Escape" && !working) {
        ev.stopPropagation();
        done(null);
      }
    };
    const done = (answer: { result: DeletedInstrument; text: string } | null) => {
      backdrop.remove();
      document.removeEventListener("keydown", onKey, true);
      resolve(answer);
    };
    both.addEventListener("change", render);
    typed.addEventListener("input", render);
    no.addEventListener("click", () => !working && done(null));
    backdrop.addEventListener("pointerdown", (ev) => {
      if (ev.target === backdrop && !working) done(null);
    });
    ok.addEventListener("click", async () => {
      if (!preview || !canDelete(preview, typed.value, both.checked)) return;
      working = true;
      ok.disabled = no.disabled = true;
      result.busy("Deleting and compacting the database…");
      try {
        const answer = await account.deleteInstrument(name, preview.token, typed.value, both.checked);
        done({ result: answer, text: deletedText(name, answer) });
        return;
      } catch (err) {
        result.fail(message(err));
        if (err instanceof ApiError && err.status === 409) await load(); // a fresh preview: look again
      } finally {
        working = false;
        no.disabled = false;
        render();
      }
    });
    document.addEventListener("keydown", onKey, true);
    document.body.append(backdrop);
    typed.focus();
    void load();
  });
}
