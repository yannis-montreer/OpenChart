/** Application shell: top bar, tool column, chart, watchlist, bottom bar. */

import {
  account,
  api,
  CLIENT_API_VERSION,
  drawingsApi,
  type Alert,
  type AlertEvent,
  type Drawing,
  type Instrument,
  type Meta,
  type Price,
  type Status,
  type TimeframeMeta,
} from "./api";
import { ChartView, type ChartContext } from "./chart/view";
import { DrawingController, type AlertPlace, type Tool } from "./drawings/controller";
import { loadPrefs, resolveTimezone, savePrefs, type Prefs } from "./prefs";
import { formatClock, offsetLabel } from "./time";
import { openAlertDialog } from "./ui/alertDialog";
import { AlertsPanel } from "./ui/alertsPanel";
import { showLogin } from "./ui/auth";
import { button, closeMenu, h, icon, installTooltips, menuItem, showMenu, showMenuAt } from "./ui/dom";
import { SettingsDialog, type Section } from "./ui/settings";
import { toast } from "./ui/toast";
import { Watchlist } from "./ui/watchlist";
import { LiveSocket, type ServerMessage, type SocketState, type SubKey } from "./ws";

const TIMEZONES = [
  "local",
  "UTC",
  "America/New_York",
  "America/Chicago",
  "Europe/London",
  "Europe/Paris",
  "Europe/Oslo",
  "Asia/Dubai",
  "Asia/Singapore",
  "Asia/Tokyo",
  "Australia/Sydney",
];

/** Date-range buttons: how far back, and which timeframe suits that span. */
const RANGES: { label: string; tf: string; days?: number; ytd?: boolean }[] = [
  { label: "1D", tf: "1m", days: 1 },
  { label: "5D", tf: "5m", days: 5 },
  { label: "1M", tf: "30m", days: 31 },
  { label: "3M", tf: "1h", days: 92 },
  { label: "6M", tf: "4h", days: 183 },
  { label: "YTD", tf: "1D", ytd: true },
  { label: "1Y", tf: "1D", days: 365 },
  { label: "All", tf: "1W" },
];

const STALE_TICKS = 120; // seconds without ticks before we call the market quiet

export class App {
  private prefs: Prefs = loadPrefs();
  private meta!: Meta;
  private instruments: Instrument[] = [];
  private instrument?: Instrument;
  private status?: Status;
  private serverOffset = 0; // server clock minus local clock, seconds
  private socket = new LiveSocket(
    `${location.protocol === "https:" ? "wss" : "ws"}://${location.host}${location.pathname.replace(/[^/]*$/, "")}ws`,
  );
  private chartKey?: SubKey;
  private settingsDialog = new SettingsDialog();
  private historyState = new Map<string, string>();

  chart!: ChartView;
  drawings!: DrawingController;
  private toolButtons = new Map<Tool, HTMLButtonElement>();
  private watchlist!: Watchlist;
  private alertsPanel = new AlertsPanel();
  private els = {} as Record<
    "symbol" | "tfGroup" | "conn" | "status" | "clock" | "tz" | "priceSeg" | "panel" | "panelBtn" | "alertsBtn" | "wlBox" | "chartArea" | "banner",
    HTMLElement
  >;

  constructor(private root: HTMLElement) {}

  async start(): Promise<void> {
    document.documentElement.dataset.theme = this.prefs.theme;
    installTooltips();
    this.build();
    try {
      [this.meta, this.instruments] = await Promise.all([api.meta(), api.instruments()]);
    } catch (err) {
      this.showBanner(`Cannot reach the OpenChart server: ${(err as Error).message}`, "bad");
      window.setTimeout(() => location.reload(), 10000);
      return;
    }
    this.serverOffset = this.meta.server_time - Date.now() / 1000;
    this.checkVersion(this.meta.api_version);
    this.instrument =
      this.instruments.find((i) => i.instrument === this.prefs.instrument) ??
      this.instruments.find((i) => i.enabled) ??
      this.instruments[0];
    if (!this.meta.timeframes.some((t) => t.name === this.prefs.timeframe)) this.prefs.timeframe = "15m";
    this.renderTimeframes();
    this.renderSymbol();
    this.renderPriceSeg();
    this.renderTzSelect();
    this.watchlist.setInstruments(this.instruments, this.instrument?.instrument);

    this.socket.onMessage = (m) => this.onMessage(m);
    this.socket.onState = (s) => this.onSocketState(s);
    this.socket.onUnauthorized = () => showLogin("Your session has ended. Please log in again.");
    this.settingsDialog.onClose = (changed) => {
      if (changed) location.reload();
    };
    this.socket.onOpen = (reconnect) => {
      if (!reconnect) return;
      void this.chart.resync();
      void this.drawings.reload(); // changes made while we were away
      void this.alertsPanel.load();
    };
    // Once a minute, re-check the last bars against the server. This picks up bars that
    // were forming while we (or the server) were disconnected, which the server stores
    // after its once-a-minute broker refresh, and swaps tick-built bars for the broker's.
    window.setInterval(() => {
      if (this.socket.state === "open" && !document.hidden) void this.chart.resync(5);
    }, 60_000);
    this.socket.start();
    for (const inst of this.instruments) this.socket.subscribe(this.watchKey(inst.instrument));
    void this.watchlist.loadPrevious(this.meta);
    void this.alertsPanel.load();

    if (!this.instrument) {
      this.chartMessage("No instruments yet. Add one in Settings, Instruments.");
      void this.openSettings("instruments");
      return;
    }
    await this.loadChart();
    window.setInterval(() => this.tickClock(), 1000);
    this.tickClock();
  }

  // Layout

  private build(): void {
    const topbar = h("header", { class: "topbar" });
    const tools = h("nav", { class: "tools", "aria-label": "Tools" });
    const chartArea = h("main", { class: "chart-area" });
    const bottombar = h("footer", { class: "bottombar" });
    const panel = h("aside", { class: "side-panel", hidden: !this.prefs.panelOpen });
    const strip = h("nav", { class: "strip", "aria-label": "Panels" });
    this.root.append(topbar, tools, chartArea, bottombar, panel, strip);
    this.els.chartArea = chartArea;
    this.els.panel = panel;

    // Top bar
    this.els.symbol = h("button", { class: "btn symbol-btn", type: "button", "data-tip": "Change instrument" });
    this.els.symbol.addEventListener("click", () => this.openSymbolMenu());
    this.els.tfGroup = h("div", { class: "tf-group", role: "group", "aria-label": "Timeframe" });
    const soon = (what: string, m: string) => `${what}: coming in ${m}`;
    this.els.conn = h("div", { class: "conn", role: "status" });
    topbar.append(
      this.els.symbol,
      h("div", { class: "sep" }),
      this.els.tfGroup,
      h("div", { class: "sep hide-narrow" }),
      button({ icon: "candles", title: soon("Chart type", "M7"), disabled: true, class: "hide-narrow" }),
      button({ icon: "search", label: "Indicators", title: soon("Indicators", "M7"), disabled: true, class: "hide-narrow" }),
      button({
        icon: "bell",
        label: "Alert",
        title: "Create alert (Alt+A): on the selected line, or at a price level",
        class: "hide-narrow",
        onClick: () => void this.addAlert(),
      }),
      h("div", { class: "spacer" }),
      button({
        icon: this.prefs.theme === "dark" ? "sun" : "moon",
        title: "Switch theme",
        onClick: (ev) => this.toggleTheme(ev.currentTarget as HTMLButtonElement),
      }),
      button({ icon: "camera", title: "Save chart image", onClick: () => this.screenshot(), class: "hide-narrow" }),
      button({ icon: "maximize", title: "Full screen", onClick: () => this.fullscreen(), class: "hide-narrow" }),
      button({ icon: "settings", title: "Settings", onClick: () => void this.openSettings() }),
      h("div", { class: "sep hide-narrow" }),
      this.els.conn,
    );

    // Chart
    this.chart = new ChartView(chartArea);
    this.drawings = new DrawingController(
      this.chart,
      chartArea.querySelector(".chart-host") as HTMLElement,
      chartArea,
      () => this.prefs.drawingStyle,
      this.prefs.drawingsHidden,
    );
    this.drawings.onToolChange = (tool) => {
      for (const [t, b] of this.toolButtons) b.classList.toggle("active", t === tool);
    };
    this.drawings.onAddAlert = (d, place) => void this.addAlert(d, place);
    this.drawings.alertsOf = (id) => this.alertsPanel.forDrawing(id);
    this.drawings.onStyleUsed = (style) => {
      this.prefs.drawingStyle = style;
      savePrefs(this.prefs);
    };

    // Left tools
    const snapHint = "Hold Ctrl to snap to the bar's open, high, low or close";
    const tool = (t: Tool, ic: Parameters<typeof icon>[0], title: string) => {
      const b = button({ icon: ic, title, onClick: () => this.drawings.setTool(this.drawings.tool === t ? "cursor" : t) });
      this.toolButtons.set(t, b);
      return b;
    };
    const eye = button({
      icon: this.prefs.drawingsHidden ? "eyeOff" : "eye",
      title: "Show or hide drawings",
      class: this.prefs.drawingsHidden ? "active" : "",
      onClick: () => this.drawings.setHidden(!this.drawings.hidden),
    });
    this.drawings.onHiddenChange = (hidden) => {
      eye.replaceChildren(icon(hidden ? "eyeOff" : "eye"));
      eye.classList.toggle("active", hidden);
      this.prefs.drawingsHidden = hidden;
      savePrefs(this.prefs);
    };
    const trash = button({ icon: "trash", title: "Delete all drawings on this instrument" });
    trash.addEventListener("click", () => this.confirmDeleteAll(trash));
    tools.append(
      tool("cursor", "crosshair", "Cursor (Esc)"),
      h("div", { class: "sep-h" }),
      tool("trendline", "trendline", `Trendline (Alt+T). ${snapHint}`),
      tool("ray", "ray", `Ray (Alt+Y). ${snapHint}`),
      tool("extended", "extended", `Extended line (Alt+E). ${snapHint}`),
      tool("hline", "hline", `Horizontal line (Alt+H). ${snapHint}`),
      h("div", { class: "sep-h" }),
      eye,
      trash,
      h("div", { class: "grow" }),
      button({ icon: "keyboard", title: "Keyboard shortcuts (?)", onClick: (ev) => this.openShortcuts(ev.currentTarget as HTMLElement) }),
    );
    this.drawings.onToolChange("cursor");
    this.els.banner = h("div", { class: "banner", hidden: true });
    chartArea.append(this.els.banner);

    // Bottom bar
    const ranges = h("div", { class: "ranges", role: "group", "aria-label": "Date range" });
    for (const r of RANGES) {
      ranges.append(button({ label: r.label, title: `Show ${r.label === "All" ? "all history" : r.label} (${r.tf} bars)`, onClick: () => void this.applyRange(r) }));
    }
    this.els.priceSeg = h("div", { class: "seg", role: "group", "aria-label": "Price" });
    this.els.status = h("span", { class: "status-text" });
    this.els.clock = h("span", { class: "clock num", "data-tip": "Server time" });
    this.els.tz = h("select", { class: "tz-select", "aria-label": "Display timezone" });
    this.els.tz.addEventListener("change", () => this.setTimezone((this.els.tz as HTMLSelectElement).value));
    bottombar.append(
      ranges,
      h("div", { class: "spacer" }),
      this.els.priceSeg,
      h("div", { class: "sep hide-narrow" }),
      h("span", { class: "hide-narrow" }, this.els.status),
      h("div", { class: "sep hide-narrow" }),
      this.els.clock,
      this.els.tz,
    );

    // Right strip and panel
    this.els.wlBox = h("div", { class: "panel-view" });
    this.watchlist = new Watchlist(this.els.wlBox, (name) => this.selectInstrument(name));
    panel.append(this.els.wlBox, this.alertsPanel.el);
    this.alertsPanel.onEdit = (a) => void this.editAlert(a);
    this.alertsPanel.onChange = () => this.drawings.alertsChanged();
    this.alertsPanel.decimals = (name) => this.instruments.find((i) => i.instrument === name)?.decimals ?? 5;
    this.els.panelBtn = button({ icon: "list", title: "Watchlist", onClick: () => this.togglePanel("watchlist") });
    this.els.alertsBtn = button({ icon: "bell", title: "Alerts and their log", onClick: () => this.togglePanel("alerts") });
    this.showPanel();
    strip.append(
      this.els.panelBtn,
      this.els.alertsBtn,
      h("div", { class: "grow" }),
      button({ icon: "info", title: "About OpenChart", onClick: (ev) => this.openAbout(ev.currentTarget as HTMLElement) }),
    );

    document.addEventListener("keydown", (ev) => this.onKey(ev));
  }

  // Chart selection

  private context(): ChartContext {
    return {
      instrument: this.instrument!,
      timeframe: this.tf(this.prefs.timeframe),
      price: this.prefs.price,
      tz: resolveTimezone(this.prefs.timezone),
      session: this.meta.session,
    };
  }

  private tf(name: string): TimeframeMeta {
    return this.meta.timeframes.find((t) => t.name === name) ?? this.meta.timeframes[0];
  }

  private watchKey(instrument: string): SubKey {
    return { instrument, timeframe: "1D", price: "mid" };
  }

  private async loadChart(opts: { from?: number; visible?: [number, number] } = {}): Promise<void> {
    if (!this.instrument) return;
    const key: SubKey = {
      instrument: this.instrument.instrument,
      timeframe: this.prefs.timeframe,
      price: this.prefs.price,
    };
    if (this.chartKey) this.socket.unsubscribe(this.chartKey);
    this.chartKey = key;
    this.socket.subscribe(key);
    savePrefs(this.prefs);
    this.updateFeedIndicators();
    void this.drawings.setInstrument(this.instrument.instrument, this.instrument.hours, this.instrument.closures ?? []);
    await this.chart.load(this.context(), opts);
    document.title = `${this.instrument.instrument} ${this.prefs.timeframe} · OpenChart`;
  }

  private selectInstrument(name: string): void {
    const inst = this.instruments.find((i) => i.instrument === name);
    if (!inst || inst === this.instrument) return;
    this.instrument = inst;
    this.prefs.instrument = name;
    this.renderSymbol();
    this.watchlist.setCurrent(name);
    void this.loadChart();
  }

  private selectTimeframe(name: string): void {
    if (name === this.prefs.timeframe) return;
    this.prefs.timeframe = name;
    this.renderTimeframes();
    void this.loadChart();
  }

  private selectPrice(price: Price): void {
    if (price === this.prefs.price) return;
    this.prefs.price = price;
    this.renderPriceSeg();
    void this.loadChart();
  }

  private async applyRange(r: (typeof RANGES)[number]): Promise<void> {
    const now = Date.now() / 1000 + this.serverOffset;
    this.prefs.timeframe = r.tf;
    this.renderTimeframes();
    if (!r.days && !r.ytd) {
      await this.loadChart();
      this.chart.chart.timeScale().fitContent();
      return;
    }
    const from = r.ytd ? Date.UTC(new Date(now * 1000).getUTCFullYear(), 0, 1) / 1000 : now - r.days! * 86400;
    await this.loadChart({ from: Math.floor(from), visible: [from, now] });
  }

  private setTimezone(tz: string): void {
    this.prefs.timezone = tz;
    savePrefs(this.prefs);
    this.renderTzSelect();
    this.chart.rerender(this.context());
    this.tickClock();
  }

  // Rendering of controls

  private renderSymbol(): void {
    const name = this.instrument?.instrument ?? "No instrument";
    this.els.symbol.replaceChildren(icon("search", 15), h("span", {}, name), h("span", { class: "spacer" }), icon("chevron", 14));
  }

  private renderTimeframes(): void {
    const group = this.els.tfGroup;
    group.replaceChildren();
    const favs = this.meta?.timeframes.filter((t) => this.prefs.favorites.includes(t.name)) ?? [];
    const current = this.prefs.timeframe;
    if (!favs.some((t) => t.name === current)) favs.push(this.tf(current));
    favs.forEach((t, i) => {
      group.append(
        button({
          label: t.name,
          title: `${describeTf(t)}${i < 9 ? ` (${i + 1})` : ""}`,
          class: t.name === current ? "active" : "",
          onClick: () => this.selectTimeframe(t.name),
        }),
      );
    });
    const more = button({ icon: "chevron", title: "All timeframes" });
    more.addEventListener("click", () => this.openTimeframeMenu(more));
    group.append(more);
  }

  private openTimeframeMenu(anchor: HTMLElement): void {
    const box = h("div");
    const groups: [string, (t: TimeframeMeta) => boolean][] = [
      ["Seconds", (t) => t.kind === "tick"],
      ["Minutes", (t) => t.kind === "fixed" && t.seconds < 3600],
      ["Hours", (t) => t.seconds >= 3600 && t.seconds < 86400],
      ["Days", (t) => t.seconds >= 86400],
    ];
    for (const [title, test] of groups) {
      box.append(h("div", { class: "menu-section" }, title));
      for (const t of this.meta.timeframes.filter(test)) {
        const fav = this.prefs.favorites.includes(t.name);
        const star = h("span", { class: `star${fav ? " on" : ""}`, title: fav ? "Remove from bar" : "Add to bar" }, icon("star", 15));
        star.addEventListener("click", (ev) => {
          ev.stopPropagation();
          const on = !this.prefs.favorites.includes(t.name);
          this.prefs.favorites = on
            ? [...this.prefs.favorites, t.name]
            : this.prefs.favorites.filter((n) => n !== t.name);
          star.classList.toggle("on", on);
          savePrefs(this.prefs);
          this.renderTimeframes();
        });
        box.append(
          menuItem({
            label: describeTf(t),
            selected: t.name === this.prefs.timeframe,
            onClick: () => this.selectTimeframe(t.name),
            extra: star,
          }),
        );
      }
    }
    showMenu(anchor, box);
  }

  private openSymbolMenu(): void {
    const box = h("div");
    box.append(h("div", { class: "menu-section" }, "Instruments"));
    for (const inst of this.instruments) {
      box.append(
        menuItem({
          label: inst.instrument,
          hint: inst.display_name,
          selected: inst === this.instrument,
          onClick: () => this.selectInstrument(inst.instrument),
        }),
      );
    }
    box.append(
      h("hr"),
      menuItem({ label: "Add or remove instruments…", icon: "plus", onClick: () => void this.openSettings("instruments") }),
    );
    showMenu(this.els.symbol, box);
  }

  private renderPriceSeg(): void {
    this.els.priceSeg.replaceChildren(
      ...(["mid", "bid", "ask"] as Price[]).map((p) =>
        button({
          label: p[0].toUpperCase() + p.slice(1),
          title: `Chart ${p} prices`,
          class: p === this.prefs.price ? "active" : "",
          onClick: () => this.selectPrice(p),
        }),
      ),
    );
  }

  private renderTzSelect(): void {
    const sel = this.els.tz as HTMLSelectElement;
    const now = Date.now() / 1000;
    const zones = TIMEZONES.includes(this.prefs.timezone) ? TIMEZONES : [...TIMEZONES, this.prefs.timezone];
    sel.replaceChildren(
      ...zones.map((z) => {
        const real = resolveTimezone(z);
        const city = z.split("/").pop()!.replace(/_/g, " ");
        const text = z === "UTC" ? "UTC" : `${offsetLabel(now, real)} ${z === "local" ? "Local" : city}`;
        return h("option", { value: z, selected: z === this.prefs.timezone, title: real }, text);
      }),
    );
    sel.title = "Display timezone";
  }

  private tickClock(): void {
    const now = Date.now() / 1000 + this.serverOffset;
    const tz = resolveTimezone(this.prefs.timezone);
    this.els.clock.textContent = formatClock(now, tz);
    this.updateFeedIndicators();
  }

  // Server messages

  private onMessage(m: ServerMessage): void {
    switch (m.type) {
      case "hello":
        this.serverOffset = m.server_time - Date.now() / 1000;
        this.checkVersion(m.api_version);
        break;
      case "status":
        this.status = m;
        this.updateFeedIndicators();
        this.watchHistory();
        break;
      case "tick":
        if (m.instrument === this.instrument?.instrument) this.chart.applyTick(m.bid, m.ask);
        this.watchlist.onTick(m.instrument, m.bid, m.ask);
        break;
      case "candle": {
        const k = this.chartKey;
        if (k && m.instrument === k.instrument && m.timeframe === k.timeframe && m.price === k.price) {
          this.chart.applyBar({ ...m.candle, complete: m.closed });
        }
        if (m.timeframe === "1D" && m.price === "mid") this.watchlist.onDaily(m.instrument, m.candle);
        break;
      }
      case "drawing_changed":
        this.drawings.applyRemote(m.op, m.drawing);
        break;
      case "alert_changed":
        this.alertsPanel.changed(m.op, m.alert);
        break;
      case "alert_triggered":
        this.alertFired(m.alert, m.event);
        break;
      case "alert_event":
        this.alertsPanel.eventChanged(m.event);
        break;
      case "closures": {
        const inst = this.instruments.find((i) => i.instrument === m.instrument);
        if (inst) inst.closures = m.closures;
        this.drawings.setClosures(m.instrument, m.closures);
        break;
      }
      case "error":
        console.warn("server:", m.error);
        break;
    }
  }

  private onSocketState(_s: SocketState): void {
    this.updateFeedIndicators();
  }

  /** One place that decides the connection dot, its label and the legend dot. */
  private updateFeedIndicators(): void {
    let cls = "dot ok";
    let label = "Live";
    let detail = "";
    const s = this.status;
    const inst = this.instrument?.instrument;
    const age = inst ? s?.instruments?.[inst]?.last_tick_age : null;
    if (this.socket.state !== "open") {
      cls = "dot warn";
      label = this.socket.state === "connecting" ? "Connecting" : "Reconnecting";
      detail = "Connection to the server lost, reconnecting";
    } else if (!s) {
      cls = "dot warn";
      label = "Waiting";
    } else if (s.provider_state === "not_configured" || s.provider_state === "no_instruments") {
      cls = "dot bad";
      label = "No feed";
      detail = s.provider_state === "not_configured" ? "No broker credentials on the server" : "No instruments enabled";
    } else if (s.error) {
      cls = "dot bad";
      label = "Feed error";
      detail = s.error;
    } else if (s.provider_state !== "connected") {
      cls = "dot warn";
      label = "Feed " + s.provider_state;
      detail = `Broker feed ${s.provider_state}`;
    } else if (inst && s.instruments?.[inst]?.history?.state === "running") {
      const h = s.instruments[inst].history!;
      cls = "dot warn";
      label = "Downloading";
      detail = `Downloading ${inst} history: ${h.written.toLocaleString("en-US")} candles so far`;
    } else if (age === null || age === undefined || age > STALE_TICKS) {
      cls = "dot";
      label = "Quiet";
      detail = age ? `No ticks for ${formatAge(age)} (market closed?)` : "No ticks yet";
    } else {
      detail = `Last tick ${formatAge(age)} ago`;
    }
    this.els.conn.replaceChildren(h("span", { class: cls }), h("span", {}, label));
    this.els.conn.dataset.tip = detail || "Connected";
    const provider = s?.provider ? s.provider.toUpperCase() : "feed";
    this.els.status.textContent = `${provider}: ${detail || label}`;
    this.chart.feedDotClass = cls;
    this.chart.renderLegend();
  }

  private checkVersion(serverVersion: number): void {
    if (serverVersion > CLIENT_API_VERSION) {
      this.showBanner(`This page is older than the server (API ${CLIENT_API_VERSION} vs ${serverVersion}). Reload to update.`, "warn");
    }
  }

  private showBanner(text: string, kind: "warn" | "bad"): void {
    this.els.banner.replaceChildren(h("span", { class: `dot ${kind}` }), h("span", {}, text));
    this.els.banner.hidden = false;
  }

  private chartMessage(text: string): void {
    this.els.chartArea.append(h("div", { class: "chart-message" }, text));
  }

  // Actions

  private confirmDeleteAll(anchor: HTMLElement): void {
    const n = this.drawings.count();
    const name = this.instrument?.instrument ?? "";
    if (n === 0) {
      showMenu(anchor, h("div", {}, h("p", {}, `No drawings on ${name}.`)), "right");
      return;
    }
    showMenu(
      anchor,
      h(
        "div",
        { style: "min-width:240px" },
        h("p", {}, `Delete all ${n} drawing${n === 1 ? "" : "s"} on ${name}? This cannot be undone.`),
        menuItem({ label: "Delete all", icon: "trash", onClick: () => void this.drawings.removeAll() }),
        menuItem({ label: "Cancel" }),
      ),
      "right",
    );
  }

  private openSettings(section?: Section): Promise<void> {
    closeMenu();
    return this.settingsDialog.open(section);
  }

  /** A history download finished for the instrument on screen: show it. */
  private watchHistory(): void {
    const s = this.status;
    if (!s?.instruments) return;
    for (const [name, info] of Object.entries(s.instruments)) {
      const state = info.history?.state ?? "";
      const before = this.historyState.get(name);
      this.historyState.set(name, state);
      if (before === "running" && state === "done" && name === this.instrument?.instrument) {
        void this.loadChart();
      }
    }
  }

  private toggleTheme(btn: HTMLButtonElement): void {
    this.prefs.theme = this.prefs.theme === "dark" ? "light" : "dark";
    document.documentElement.dataset.theme = this.prefs.theme;
    btn.replaceChildren(icon(this.prefs.theme === "dark" ? "sun" : "moon"));
    savePrefs(this.prefs);
    this.chart.setTheme();
  }

  /** The strip buttons: open a view, or close the panel if it already shows it. */
  private togglePanel(view: Prefs["panelView"]): void {
    this.prefs.panelOpen = !(this.prefs.panelOpen && this.prefs.panelView === view);
    this.prefs.panelView = view;
    this.showPanel();
    savePrefs(this.prefs);
  }

  private showPanel(): void {
    const { panelOpen, panelView } = this.prefs;
    this.els.panel.hidden = !panelOpen;
    this.els.wlBox.hidden = panelView !== "watchlist";
    this.alertsPanel.el.hidden = panelView !== "alerts";
    this.els.panelBtn.classList.toggle("active", panelOpen && panelView === "watchlist");
    this.els.alertsBtn.classList.toggle("active", panelOpen && panelView === "alerts");
  }

  // Alerts

  private async telegramReady(): Promise<boolean> {
    try {
      const s = await account.settings();
      return s.telegram.token.set && !!s.telegram.chat_id;
    } catch {
      return false;
    }
  }

  /**
   * The Alert button, Alt+A and a line's bell or menu: on `drawing` (or the selected line),
   * else a new alert at the current price. A line that already has alerts opens its alert
   * for editing, or a short list when it has several.
   */
  private async addAlert(drawing?: Drawing, place?: AlertPlace): Promise<void> {
    if (!this.instrument) return;
    const selected = this.drawings.selectedId ? this.drawings.get(this.drawings.selectedId) : undefined;
    const line = drawing ?? selected;
    const existing = line ? this.alertsPanel.forDrawing(line.id) : [];
    if (existing.length === 1) return this.editAlert(existing[0]);
    if (existing.length > 1) return this.pickAlert(existing, place);
    openAlertDialog({
      target: {
        instrument: this.instrument.instrument,
        timeframe: this.prefs.timeframe,
        price: this.prefs.price,
        decimals: this.instrument.decimals,
        drawing: line,
        level: line ? undefined : this.chart.lastClose(),
      },
      telegramReady: await this.telegramReady(),
      onSaved: (a) => this.alertsPanel.changed("update", a),
    });
  }

  /** Several alerts on one line: pick the one to edit. */
  private pickAlert(alerts: Alert[], place?: AlertPlace): void {
    const status = { active: "Active", triggered: "Triggered", expired: "Expired", stopped: "Stopped" } as const;
    const box = h(
      "div",
      { class: "select-menu", style: "min-width:260px", "aria-label": "Alerts on this line" },
      ...alerts.map((a) =>
        menuItem({
          label: a.name,
          icon: a.status === "active" ? "bellRing" : "bellOff",
          extra: h("span", { class: "al-pick-status" }, status[a.status]),
          onClick: () => void this.editAlert(a),
        }),
      ),
    );
    if (place?.anchor) showMenu(place.anchor, box);
    else {
      const r = this.els.chartArea.getBoundingClientRect();
      const at = place?.at ?? { x: r.left + r.width / 2, y: r.top + r.height / 3 };
      showMenuAt(at.x, at.y, box);
    }
  }

  private async editAlert(alert: Alert): Promise<void> {
    const inst = this.instruments.find((i) => i.instrument === alert.instrument);
    let drawing: Drawing | undefined;
    if (alert.drawing_id) {
      drawing = this.drawings.get(alert.drawing_id);
      if (!drawing) {
        try {
          drawing = (await drawingsApi.list(alert.instrument)).find((d) => d.id === alert.drawing_id);
        } catch {
          /* the dialog then calls it "Line" */
        }
      }
    }
    openAlertDialog({
      alert,
      target: {
        instrument: alert.instrument,
        timeframe: alert.timeframe,
        price: alert.trigger_price,
        decimals: inst?.decimals ?? 5,
        drawing,
      },
      telegramReady: await this.telegramReady(),
      onSaved: (a) => this.alertsPanel.changed("update", a),
      onDeleted: (a) => this.alertsPanel.changed("delete", a),
    });
  }

  private alertFired(alert: Alert, event: AlertEvent): void {
    this.alertsPanel.triggered(alert, event);
    if (!alert.notify.includes("app")) return;
    const inst = this.instruments.find((i) => i.instrument === alert.instrument);
    const d = inst?.decimals ?? 5;
    const fmt = (x: number | null) => (x === null ? "–" : x.toFixed(d));
    toast({
      title: alert.name,
      body: `${alert.instrument} crossed ${event.cross}: ${fmt(event.price)} (line ${fmt(event.line_value)})`,
      note: event.late ? "Found late: the server was asleep, offline or restarting." : undefined,
      sound: true,
    });
  }

  private screenshot(): void {
    const canvas = this.chart.screenshot();
    canvas.toBlob((blob) => {
      if (!blob) return;
      const a = h("a", {
        href: URL.createObjectURL(blob),
        download: `openchart-${this.instrument?.instrument}-${this.prefs.timeframe}-${new Date().toISOString().slice(0, 19).replace(/[:T]/g, "")}.png`,
      });
      a.click();
      window.setTimeout(() => URL.revokeObjectURL(a.href), 1000);
    });
  }

  private fullscreen(): void {
    if (document.fullscreenElement) void document.exitFullscreen();
    else void document.documentElement.requestFullscreen?.();
  }

  private openShortcuts(anchor: HTMLElement): void {
    const row = (keys: string, what: string) => h("div", { class: "kbd-row" }, h("span", {}, what), h("kbd", {}, keys));
    showMenu(
      anchor,
      h(
        "div",
        { style: "min-width:280px" },
        h("div", { class: "menu-section" }, "Keyboard shortcuts"),
        row("1 – 9", "Timeframes in the top bar"),
        row("[  ]", "Previous / next timeframe"),
        row("Alt + R", "Reset chart view"),
        row("Esc", "Cancel drawing, back to the cursor"),
        row("Alt + T / Y / E / H", "Trendline, ray, extended line, horizontal line"),
        row("Ctrl (while drawing)", "Snap to the bar's open, high, low or close"),
        row("Del", "Delete the selected drawing"),
        row("Alt + A", "Alert on the selected line, or at a price level"),
        row("?", "This list"),
        h("hr"),
        h("p", {}, "Mouse: wheel to zoom, drag to pan, drag the price scale to stretch it, double-click it to fit."),
      ),
      "right",
    );
  }

  private openAbout(anchor: HTMLElement): void {
    showMenu(
      anchor,
      h(
        "div",
        { style: "max-width:320px" },
        h("div", { class: "menu-section" }, "OpenChart"),
        h("p", {}, "Self-hosted charting and alerting. Independent project, not affiliated with TradingView. Not financial advice; no warranty."),
        h(
          "p",
          {},
          "Charts by ",
          h("a", { href: "https://www.tradingview.com/", target: "_blank", rel: "noopener" }, "TradingView Lightweight Charts™"),
          " (Apache 2.0). Icons by ",
          h("a", { href: "https://lucide.dev/", target: "_blank", rel: "noopener" }, "Lucide"),
          " (ISC).",
        ),
        h("p", {}, `Client API ${CLIENT_API_VERSION}, server API ${this.meta?.api_version ?? "?"}.`),
      ),
      "above-left",
    );
  }

  private onKey(ev: KeyboardEvent): void {
    const target = ev.target;
    if (target instanceof Element && target.closest("input, select, textarea, [contenteditable]")) return;
    if (ev.key === "Escape") {
      closeMenu();
      return;
    }
    if (ev.altKey && (ev.key === "a" || ev.key === "A")) {
      ev.preventDefault();
      void this.addAlert();
      return;
    }
    if (ev.altKey && (ev.key === "r" || ev.key === "R")) {
      ev.preventDefault();
      this.chart.resetView();
      return;
    }
    if (ev.ctrlKey || ev.metaKey || ev.altKey || !this.meta) return;
    if (/^[1-9]$/.test(ev.key)) {
      const favs = this.meta.timeframes.filter((t) => this.prefs.favorites.includes(t.name));
      const t = favs[Number(ev.key) - 1];
      if (t) this.selectTimeframe(t.name);
    } else if (ev.key === "[" || ev.key === "]") {
      const all = this.meta.timeframes;
      const i = all.findIndex((t) => t.name === this.prefs.timeframe);
      const next = all[Math.max(0, Math.min(all.length - 1, i + (ev.key === "]" ? 1 : -1)))];
      this.selectTimeframe(next.name);
    } else if (ev.key === "?") {
      this.openShortcuts(this.root.querySelector(".tools .grow + .btn") as HTMLElement);
    }
  }
}

function describeTf(t: TimeframeMeta): string {
  const n = parseInt(t.name, 10);
  const unit = t.name.replace(/^\d+/, "");
  const words: Record<string, string> = { s: "second", m: "minute", h: "hour", D: "day", W: "week" };
  return `${n} ${words[unit] ?? unit}${n === 1 ? "" : "s"}`;
}

function formatAge(seconds: number): string {
  if (seconds < 60) return `${Math.round(seconds)} s`;
  if (seconds < 3600) return `${Math.round(seconds / 60)} min`;
  if (seconds < 86400) return `${Math.round(seconds / 3600)} h`;
  return `${Math.round(seconds / 86400)} d`;
}
