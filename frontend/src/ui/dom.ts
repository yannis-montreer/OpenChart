/** Small DOM helpers, icons (Lucide), menus and tooltips. */

import {
  Activity,
  Bell,
  ChartArea,
  ChartLine,
  Ellipsis,
  LayoutTemplate,
  Save,
  BellOff,
  BellRing,
  Clock,
  History,
  Pause,
  Pencil,
  Play,
  Shuffle,
  TrendingDown,
  TrendingUp,
  TriangleAlert,
  Camera,
  ChartCandlestick,
  Check,
  ChevronDown,
  CircleCheck,
  Copy,
  CircleX,
  Crosshair,
  Database,
  Eye,
  EyeOff,
  Globe,
  Info,
  Keyboard,
  KeyRound,
  Landmark,
  List,
  LoaderCircle,
  Lock,
  Palette,
  LogOut,
  Magnet,
  Maximize2,
  Minus,
  Moon,
  MoveDiagonal,
  MoveUpRight,
  Plug,
  Plus,
  Search,
  Send,
  Settings,
  ShieldCheck,
  Slash,
  Star,
  Sun,
  Trash,
  Type,
  X,
  createElement,
  type IconNode,
} from "lucide";

/** OHLC bars and Heikin Ashi candles (Lucide has neither), drawn in the same 24px grid. */
const OhlcBars: IconNode = [
  ["path", { d: "M7 3v18" }],
  ["path", { d: "M4 7h3" }],
  ["path", { d: "M7 16h3" }],
  ["path", { d: "M17 6v14" }],
  ["path", { d: "M14 9h3" }],
  ["path", { d: "M17 15h3" }],
];
const HeikinAshi: IconNode = [
  ["path", { d: "M7 2v4" }],
  ["rect", { x: "4", y: "6", width: "6", height: "10", rx: "1", fill: "currentColor" }],
  ["path", { d: "M7 16v3" }],
  ["path", { d: "M17 5v4" }],
  ["rect", { x: "14", y: "9", width: "6", height: "9", rx: "1" }],
  ["path", { d: "M17 18v4" }],
];

export const icons = {
  indicators: Activity,
  chartLine: ChartLine,
  chartArea: ChartArea,
  ohlc: OhlcBars,
  heikinAshi: HeikinAshi,
  more: Ellipsis,
  layout: LayoutTemplate,
  save: Save,
  bell: Bell,
  bellOff: BellOff,
  bellRing: BellRing,
  clock: Clock,
  history: History,
  pause: Pause,
  pencil: Pencil,
  play: Play,
  crossing: Shuffle,
  crossUp: TrendingUp,
  crossDown: TrendingDown,
  warn: TriangleAlert,
  camera: Camera,
  candles: ChartCandlestick,
  check: Check,
  copy: Copy,
  palette: Palette,
  text: Type,
  chevron: ChevronDown,
  crosshair: Crosshair,
  database: Database,
  eye: Eye,
  eyeOff: EyeOff,
  globe: Globe,
  key: KeyRound,
  bank: Landmark,
  lock: Lock,
  logout: LogOut,
  ok: CircleCheck,
  fail: CircleX,
  loader: LoaderCircle,
  plug: Plug,
  plus: Plus,
  send: Send,
  shield: ShieldCheck,
  x: X,
  info: Info,
  keyboard: Keyboard,
  list: List,
  magnet: Magnet,
  maximize: Maximize2,
  hline: Minus,
  moon: Moon,
  extended: MoveDiagonal,
  ray: MoveUpRight,
  search: Search,
  settings: Settings,
  trendline: Slash,
  star: Star,
  sun: Sun,
  trash: Trash,
} satisfies Record<string, IconNode>;

export type IconName = keyof typeof icons;

export function icon(name: IconName, size = 18): SVGElement {
  const svg = createElement(icons[name], { width: size, height: size });
  svg.classList.add("icon");
  return svg;
}

type Child = Node | string | null | undefined | false;

export function h<K extends keyof HTMLElementTagNameMap>(
  tag: K,
  props: Partial<Record<string, unknown>> & { class?: string } = {},
  ...children: Child[]
): HTMLElementTagNameMap[K] {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(props)) {
    if (v === undefined || v === null || v === false) continue;
    if (k === "class") el.className = String(v);
    else if (k.startsWith("on") && typeof v === "function") {
      el.addEventListener(k.slice(2).toLowerCase(), v as EventListener);
    } else if (k in el && typeof v !== "string") (el as unknown as Record<string, unknown>)[k] = v;
    else el.setAttribute(k, v === true ? "" : String(v));
  }
  for (const c of children) if (c) el.append(c);
  return el;
}

/** A toolbar button with an icon and/or label and a tooltip. */
export function button(opts: {
  icon?: IconName;
  label?: string;
  title?: string;
  class?: string;
  disabled?: boolean;
  onClick?: (ev: MouseEvent) => void;
}): HTMLButtonElement {
  const b = h("button", {
    class: `btn${opts.label ? "" : " icon-only"}${opts.class ? " " + opts.class : ""}`,
    "aria-label": opts.title ?? opts.label,
    "data-tip": opts.title,
    disabled: opts.disabled,
    type: "button",
  });
  if (opts.icon) b.append(icon(opts.icon));
  if (opts.label) b.append(h("span", {}, opts.label));
  if (opts.onClick) b.addEventListener("click", opts.onClick);
  return b;
}

// Menus

let openMenu: { el: HTMLElement; anchor: HTMLElement; close: () => void } | null = null;

export function closeMenu(): void {
  openMenu?.close();
}

/** Show `content` as a menu under `anchor` (or to its right with side="right"). */
export function showMenu(
  anchor: HTMLElement,
  content: HTMLElement,
  side: "below" | "right" | "above-left" = "below",
): void {
  if (openMenu?.anchor === anchor) {
    closeMenu();
    return;
  }
  closeMenu();
  const el = h("div", { class: "menu", role: "menu" }, content);
  document.body.append(el);
  const r = anchor.getBoundingClientRect();
  const w = el.offsetWidth;
  const hgt = el.offsetHeight;
  let left = side === "right" ? r.right + 6 : r.left;
  let top = side === "right" ? r.top : r.bottom + 4;
  if (side === "above-left") {
    left = r.right - w;
    top = r.top - hgt - 6;
  }
  left = Math.max(6, Math.min(left, window.innerWidth - w - 6));
  top = Math.max(6, Math.min(top, window.innerHeight - hgt - 6));
  el.style.left = `${left}px`;
  el.style.top = `${top}px`;
  anchor.classList.add("active");

  const onDown = (ev: Event) => {
    if (!el.contains(ev.target as Node) && !anchor.contains(ev.target as Node)) close();
  };
  const onKey = (ev: KeyboardEvent) => {
    if (ev.key === "Escape") close();
  };
  const close = () => {
    el.remove();
    anchor.classList.remove("active");
    document.removeEventListener("pointerdown", onDown, true);
    document.removeEventListener("keydown", onKey, true);
    if (openMenu?.el === el) openMenu = null;
  };
  document.addEventListener("pointerdown", onDown, true);
  document.addEventListener("keydown", onKey, true);
  openMenu = { el, anchor, close };
}

/** Show `content` as a menu at a screen point (right-click menus). */
export function showMenuAt(x: number, y: number, content: HTMLElement): void {
  closeMenu();
  let point = document.getElementById("menu-point");
  if (!point) {
    point = h("div", { id: "menu-point", style: "position:fixed;width:1px;height:1px;pointer-events:none" });
    document.body.append(point);
  }
  point.style.left = `${x}px`;
  point.style.top = `${y - 4}px`;
  showMenu(point, content);
}

export function menuItem(opts: {
  label: string;
  hint?: string;
  icon?: IconName;
  selected?: boolean;
  disabled?: boolean;
  onClick?: () => void;
  extra?: HTMLElement;
}): HTMLButtonElement {
  const b = h("button", {
    class: `menu-item${opts.selected ? " selected" : ""}`,
    role: "menuitem",
    type: "button",
    disabled: opts.disabled,
  });
  if (opts.icon) b.append(icon(opts.icon, 16));
  b.append(h("span", { class: "label" }, opts.label));
  if (opts.hint) b.append(h("span", { class: "hint" }, opts.hint));
  if (opts.extra) b.append(opts.extra);
  if (opts.onClick) {
    b.addEventListener("click", (ev) => {
      if ((ev.target as HTMLElement).closest(".star")) return;
      opts.onClick?.();
      closeMenu();
    });
  }
  return b;
}

// Tooltips for anything with data-tip

export function installTooltips(): void {
  const tip = h("div", { class: "tooltip", hidden: true });
  document.body.append(tip);
  let current: HTMLElement | null = null;
  let timer = 0;
  document.addEventListener("pointerover", (ev) => {
    const el = (ev.target as HTMLElement).closest<HTMLElement>("[data-tip]");
    if (el === current) return;
    current = el;
    window.clearTimeout(timer);
    tip.hidden = true;
    if (!el || ev.pointerType === "touch") return;
    timer = window.setTimeout(() => {
      if (current !== el || document.querySelector(".menu")) return;
      tip.textContent = el.dataset.tip ?? "";
      tip.hidden = false;
      const r = el.getBoundingClientRect();
      const vertical = el.closest(".tools, .strip");
      let left = vertical ? r.right + 8 : r.left + r.width / 2 - tip.offsetWidth / 2;
      let top = vertical ? r.top + r.height / 2 - tip.offsetHeight / 2 : r.bottom + 6;
      if (el.closest(".bottombar")) top = r.top - tip.offsetHeight - 6;
      if (el.closest(".strip")) left = r.left - tip.offsetWidth - 8;
      tip.style.left = `${Math.max(4, Math.min(left, window.innerWidth - tip.offsetWidth - 4))}px`;
      tip.style.top = `${Math.max(4, top)}px`;
    }, 450);
  });
  document.addEventListener("pointerdown", () => {
    tip.hidden = true;
  });
}
