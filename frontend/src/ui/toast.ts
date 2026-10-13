/** Toasts (top right of the chart) and a short chime, for alerts that fire while the page is open. */

import { h, icon, type IconName } from "./dom";

let stack: HTMLElement | null = null;
let audio: AudioContext | null = null;

export function toast(opts: { title: string; body: string; icon?: IconName; note?: string; sound?: boolean; timeout?: number }): void {
  if (!stack?.isConnected) {
    stack = h("div", { class: "toasts", role: "log", "aria-live": "assertive" });
    // Over the chart, left of the price scale, so panels stay usable.
    (document.querySelector(".chart-area") ?? document.body).append(stack);
  }
  const el = h(
    "div",
    { class: "toast" },
    icon(opts.icon ?? "bellRing", 18),
    h(
      "div",
      { class: "toast-text" },
      h("strong", {}, opts.title),
      h("span", {}, opts.body),
      opts.note ? h("small", {}, opts.note) : null,
    ),
  );
  const close = () => el.remove();
  el.addEventListener("click", close);
  stack.prepend(el);
  window.setTimeout(close, opts.timeout ?? 15_000);
  if (opts.sound) chime();
}

/** Two short notes. Browsers only allow sound after the user interacted with the page. */
function chime(): void {
  try {
    audio ??= new AudioContext();
    const t = audio.currentTime;
    for (const [i, freq] of [880, 1320].entries()) {
      const osc = audio.createOscillator();
      const gain = audio.createGain();
      osc.frequency.value = freq;
      gain.gain.setValueAtTime(0.0001, t + i * 0.16);
      gain.gain.exponentialRampToValueAtTime(0.15, t + i * 0.16 + 0.02);
      gain.gain.exponentialRampToValueAtTime(0.0001, t + i * 0.16 + 0.15);
      osc.connect(gain).connect(audio.destination);
      osc.start(t + i * 0.16);
      osc.stop(t + i * 0.16 + 0.16);
    }
  } catch {
    /* no audio */
  }
}
