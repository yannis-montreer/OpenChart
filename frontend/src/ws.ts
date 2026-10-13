/**
 * Reconnecting websocket to /ws.
 *
 * Keeps the set of wanted subscriptions and replays it after every reconnect. A socket
 * that stays silent for `staleAfter` ms (laptop sleep, dead Wi-Fi) is treated as dead:
 * the server sends status every 5 s, so silence means the link is gone.
 */

import type { Alert, AlertEvent, Bar, Drawing, LayoutSummary, Price, Status } from "./api";
import type { Closure } from "./drawings/tradingTime";

export interface SubKey {
  instrument: string;
  timeframe: string;
  price: Price;
}

export type ServerMessage =
  | { type: "hello"; api_version: number; server_time: number }
  | ({ type: "status" } & Status)
  | { type: "tick"; instrument: string; t: number; bid: number; ask: number }
  | {
      type: "candle";
      instrument: string;
      timeframe: string;
      price: Price;
      candle: Bar;
      closed: boolean;
    }
  | { type: "subscribed" | "unsubscribed"; instrument: string; timeframe: string; price: Price }
  | { type: "pong"; server_time: number }
  | { type: "drawing_changed"; op: "create" | "update" | "delete"; drawing: Drawing }
  | { type: "closures"; instrument: string; closures: Closure[] }
  | { type: "alert_changed"; op: "create" | "update" | "delete"; alert: Alert }
  | { type: "alert_triggered"; alert: Alert; event: AlertEvent }
  | { type: "alert_event"; event: AlertEvent }
  | { type: "layouts_changed"; layouts: LayoutSummary[] }
  | { type: "error"; error: string };

export type SocketState = "connecting" | "open" | "reconnecting";

const keyOf = (k: SubKey) => `${k.instrument}|${k.timeframe}|${k.price}`;

export class LiveSocket {
  state: SocketState = "connecting";
  /** Called for every message from the server. */
  onMessage: (m: ServerMessage) => void = () => {};
  /** Called on state changes. */
  onState: (s: SocketState) => void = () => {};
  /** Called when a connection opens; `reconnect` is false only the first time. */
  onOpen: (reconnect: boolean) => void = () => {};
  /** Called when the server refuses the connection because the session is gone. */
  onUnauthorized: () => void = () => {};

  private ws?: WebSocket;
  private subs = new Map<string, { key: SubKey; refs: number }>();
  private attempt = 0;
  private opened = false;
  private lastMessage = 0;
  private timer?: number;
  private watchdog?: number;
  private stopped = false;

  constructor(
    private url: string,
    private staleAfter = 15000,
  ) {}

  start(): void {
    this.connect();
    this.watchdog = window.setInterval(() => this.check(), 2000);
  }

  stop(): void {
    this.stopped = true;
    window.clearInterval(this.watchdog);
    window.clearTimeout(this.timer);
    this.ws?.close();
  }

  /** Reference counted: the chart and the watchlist may want the same stream. */
  subscribe(k: SubKey): void {
    const entry = this.subs.get(keyOf(k));
    if (entry) {
      entry.refs += 1;
      return;
    }
    this.subs.set(keyOf(k), { key: k, refs: 1 });
    this.send({ type: "subscribe", ...k });
  }

  unsubscribe(k: SubKey): void {
    const entry = this.subs.get(keyOf(k));
    if (!entry) return;
    entry.refs -= 1;
    if (entry.refs <= 0) {
      this.subs.delete(keyOf(k));
      this.send({ type: "unsubscribe", ...k });
    }
  }

  /** Drop the connection now and reconnect (used by tests and the dead-link check). */
  reconnectNow(): void {
    this.ws?.close();
  }

  private send(m: object): void {
    if (this.ws?.readyState === WebSocket.OPEN) this.ws.send(JSON.stringify(m));
  }

  private setState(s: SocketState): void {
    if (s !== this.state) {
      this.state = s;
      this.onState(s);
    }
  }

  private connect(): void {
    const ws = new WebSocket(this.url);
    this.ws = ws;
    ws.onopen = () => {
      this.attempt = 0;
      this.lastMessage = Date.now();
      for (const { key } of this.subs.values()) this.send({ type: "subscribe", ...key });
      this.setState("open");
      this.onOpen(this.opened);
      this.opened = true;
    };
    ws.onmessage = (ev) => {
      this.lastMessage = Date.now();
      try {
        this.onMessage(JSON.parse(ev.data as string) as ServerMessage);
      } catch (err) {
        console.error("bad message", err);
      }
    };
    ws.onclose = (ev) => {
      if (this.ws !== ws || this.stopped) return;
      if (ev.code === 4401) {
        this.stop();
        this.onUnauthorized();
        return;
      }
      this.setState("reconnecting");
      const delay = Math.min(15000, 500 * 2 ** this.attempt) * (0.6 + Math.random() * 0.4);
      this.attempt += 1;
      this.timer = window.setTimeout(() => this.connect(), delay);
    };
  }

  private check(): void {
    if (this.ws?.readyState !== WebSocket.OPEN) return;
    const quiet = Date.now() - this.lastMessage;
    if (quiet > this.staleAfter) this.ws.close();
    else if (quiet > 6000) this.send({ type: "ping" }); // status should have arrived by now
  }
}
