/** REST client for the OpenChart server (see docs/api.md). */

import type { DrawingType, Point } from "./drawings/geometry";
import type { Closure, TradingHoursJson } from "./drawings/tradingTime";
import type { SessionMeta } from "./time";

/** The API version this client was written against. */
export const CLIENT_API_VERSION = 1;

export type Price = "mid" | "bid" | "ask";

export interface Bar {
  t: number;
  o: number;
  h: number;
  l: number;
  c: number;
  v: number;
  complete?: boolean;
}

export interface TimeframeMeta {
  name: string;
  seconds: number;
  kind: "tick" | "fixed" | "session";
}

export interface Meta {
  api_version: number;
  server_time: number;
  prices: Price[];
  timeframes: TimeframeMeta[];
  session: SessionMeta;
}

export interface Instrument {
  instrument: string;
  source: string;
  display_name: string;
  decimals: number;
  pip_size: number;
  enabled: boolean;
  hours: TradingHoursJson | null;
  /** Learned closures (holidays, early closes): [start, end) in UTC epoch seconds. */
  closures: Closure[];
}

export interface InstrumentStatus {
  last_tick_age: number | null;
  history: { state: string; written: number; at: number | null } | null;
}

export interface Status {
  provider: string;
  provider_state: string;
  reconnects?: number;
  error?: string | null;
  instruments: Record<string, InstrumentStatus>;
}

export interface CandleQuery {
  instrument: string;
  timeframe: string;
  price: Price;
  from?: number;
  to?: number;
  limit?: number;
}

/** Raised for HTTP errors; `status` 401 means the session is gone. */
export class ApiError extends Error {
  constructor(
    public status: number,
    message: string,
  ) {
    super(message);
  }
}

/** Fired on any 401, so the app can show the login screen. */
export const UNAUTHORIZED_EVENT = "openchart:unauthorized";

async function request<T>(
  method: string,
  path: string,
  opts: { params?: Record<string, string | number | undefined>; body?: unknown; headers?: Record<string, string> } = {},
): Promise<T> {
  const url = new URL(path, window.location.href);
  for (const [k, v] of Object.entries(opts.params ?? {})) {
    if (v !== undefined) url.searchParams.set(k, String(v));
  }
  const res = await fetch(url, {
    method,
    credentials: "same-origin",
    headers: {
      Accept: "application/json",
      ...(opts.body !== undefined ? { "Content-Type": "application/json" } : {}),
      ...opts.headers,
    },
    body: opts.body !== undefined ? JSON.stringify(opts.body) : undefined,
  });
  if (!res.ok) {
    let detail = res.statusText;
    try {
      const body = await res.json();
      detail = typeof body.detail === "string" ? body.detail : (body.detail?.[0]?.msg ?? detail);
    } catch {
      /* not JSON */
    }
    if (res.status === 401 && !path.includes("login")) {
      window.dispatchEvent(new Event(UNAUTHORIZED_EVENT));
    }
    throw new ApiError(res.status, detail);
  }
  return res.json() as Promise<T>;
}

const get = <T>(path: string, params?: Record<string, string | number | undefined>) =>
  request<T>("GET", path, { params });

export const api = {
  meta: () => get<Meta>("api/meta"),
  status: () => get<Status>("api/status"),
  instruments: () => get<Instrument[]>("api/instruments"),
  candles: async (q: CandleQuery): Promise<Bar[]> => {
    const body = await get<{ candles: Bar[] }>("api/candles", { ...q });
    return body.candles;
  },
};

// Setup, login and settings

export interface SecretInfo {
  set: boolean;
  masked: string;
  source: "saved" | "environment" | null;
}

export interface Settings {
  oanda: { environment: "practice" | "live"; account_id: string; token: SecretInfo };
  telegram: { chat_id: string; token: SecretInfo };
  store_ticks: boolean;
  timezone: string;
  listen_on_network: boolean;
  secret_store: string | null;
  secret_store_error: string | null;
  restart_required?: boolean;
}

export interface SetupStatus {
  setup_required: boolean;
  authenticated: boolean;
  setup_code_required: boolean;
  secret_store_error: string | null;
}

export interface TestOutcome {
  ok: boolean;
  message: string;
}

export interface AvailableInstrument {
  instrument: string;
  display_name: string;
  kind: string;
  enabled: boolean;
}

const code = (setupCode?: string) => (setupCode ? { "X-Setup-Code": setupCode } : undefined);

export const account = {
  setupStatus: () => get<SetupStatus>("api/setup/status"),
  setupDefaults: (setupCode?: string) =>
    request<Settings>("GET", "api/setup/defaults", { headers: code(setupCode) }),
  setup: (body: Record<string, string>) => request<{ ok: true }>("POST", "api/setup", { body }),
  login: (password: string) => request<{ ok: true }>("POST", "api/login", { body: { password } }),
  logout: () => request<{ ok: true }>("POST", "api/logout"),
  changePassword: (current: string, next: string) =>
    request<{ ok: true }>("POST", "api/password", { body: { current, new: next } }),
  settings: () => get<Settings>("api/settings"),
  saveSettings: (body: Record<string, unknown>) => request<Settings>("PUT", "api/settings", { body }),
  testOanda: (body: Record<string, string>, setupCode?: string) =>
    request<TestOutcome>("POST", "api/settings/test/oanda", { body, headers: code(setupCode) }),
  testTelegram: (body: Record<string, string>, setupCode?: string) =>
    request<TestOutcome>("POST", "api/settings/test/telegram", { body, headers: code(setupCode) }),
  available: (q: string) => get<AvailableInstrument[]>("api/instruments/available", { q }),
  addInstrument: (instrument: string) =>
    request<{ ok: true }>("POST", "api/instruments", { body: { instrument } }),
  removeInstrument: (instrument: string) =>
    request<{ ok: true }>("DELETE", `api/instruments/${encodeURIComponent(instrument)}`),
};

// Drawings

export interface DrawingStyle {
  color: string;
  width: number;
  dash: "solid" | "dashed" | "dotted";
}

export interface Drawing {
  id: string;
  instrument: string;
  type: DrawingType;
  points: Point[];
  style: DrawingStyle;
  label: string | null;
  created_at: number;
  updated_at: number;
}

export interface DrawingPatch {
  points?: Point[];
  style?: Partial<DrawingStyle>;
  label?: string;
  clear_label?: boolean;
}

export const drawingsApi = {
  list: (instrument: string) => get<Drawing[]>("api/drawings", { instrument }),
  create: (body: { instrument: string; type: DrawingType; points: Point[]; style: DrawingStyle; label?: string | null }) =>
    request<Drawing>("POST", "api/drawings", { body }),
  update: (id: string, body: DrawingPatch) => request<Drawing>("PUT", `api/drawings/${id}`, { body }),
  remove: (id: string) => request<Drawing>("DELETE", `api/drawings/${id}`),
  removeAll: (instrument: string) =>
    request<{ deleted: number }>("DELETE", "api/drawings", { params: { instrument } }),
};

// Alerts (SPEC section 9)

export type AlertDirection = "cross" | "up" | "down";
export type AlertFrequency = "once" | "once_per_bar" | "repeating";
export type AlertMode = "touch" | "candle_close";
export type AlertChannel = "telegram" | "app";

export interface Alert {
  id: string;
  instrument: string;
  type: "line_cross" | "level_cross";
  drawing_id: string | null;
  level: number | null;
  direction: AlertDirection;
  trigger_price: Price;
  trigger_mode: AlertMode;
  timeframe: string;
  frequency: AlertFrequency;
  cooldown_s: number;
  name: string;
  expires_at: number | null;
  notify: AlertChannel[];
  status: "active" | "triggered" | "expired" | "stopped";
  created_at: number;
  updated_at: number;
  last_fired_at: number | null;
  fire_count: number;
}

export interface AlertEvent {
  id: number;
  alert_id: string;
  ts: number;
  price: number;
  line_value: number | null;
  cross: "up" | "down";
  message: string;
  late: "wake" | "reconnect" | "restart" | null;
  detected_at: number;
  delivered: boolean;
  attempts: number;
  error: string | null;
}

export type AlertInput = Partial<
  Pick<
    Alert,
    "direction" | "trigger_price" | "trigger_mode" | "timeframe" | "frequency" | "cooldown_s" | "level" | "name" | "expires_at" | "notify"
  >
> & { status?: "active" | "stopped"; clear_expiry?: boolean };

export const alertsApi = {
  list: () => get<Alert[]>("api/alerts"),
  events: (limit = 100) => get<AlertEvent[]>("api/alerts/events", { limit: String(limit) }),
  create: (body: AlertInput & { instrument: string; type: Alert["type"]; drawing_id?: string }) =>
    request<Alert>("POST", "api/alerts", { body }),
  update: (id: string, body: AlertInput) => request<Alert>("PUT", `api/alerts/${id}`, { body }),
  remove: (id: string) => request<Alert>("DELETE", `api/alerts/${id}`),
};
