import { button, h } from "./dom";

/**
 * A modal with a title, a close button and Escape / backdrop to close. Escape leaves it
 * open while a menu or a confirmation (`.confirm-backdrop`) is above it: that one takes the key.
 */
export function modal(cls: string, title: string, onClose: () => void) {
  const box = h("div", { class: `modal ${cls}`, role: "dialog", "aria-modal": "true", "aria-label": title });
  const backdrop = h("div", { class: "modal-backdrop" }, box);
  const onKey = (ev: KeyboardEvent) => {
    if (ev.key === "Escape" && !document.querySelector(".menu, .confirm-backdrop")) {
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
