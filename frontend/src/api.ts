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

/** An instrument's history download (newest first: hourly, then minute candles). */
export interface HistoryStatus {
  state: "pending" | "running" | "failed" | "done";
  phase: "h1" | "m1" | null;
  written: number;
  /** Oldest candle reached so far (UTC epoch seconds). */
  oldest: number | null;
  /** Where the minute download stops. */
  target: number | null;
  /** 0..1 of the current step. */
  progress: number | null;
  error: string | null;
}

export interface InstrumentStatus {
  last_tick_age: number | null;
  history: HistoryStatus | null;
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
  secret_store: string | null;
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

// Indicators (docs/api.md, docs/indicators.md)

export type ParamType = "int" | "float" | "bool" | "choice" | "source" | "color" | "width" | "string";
export type ParamValue = number | string | boolean;
export type IndicatorParams = Record<string, ParamValue>;

export interface IndicatorParam {
  name: string;
  type: ParamType;
  default: ParamValue;
  label: string;
  group: "inputs" | "style";
  min?: number;
  max?: number;
  step?: number;
  max_length?: number; // string inputs
  choices?: string[];
}

export type LineStyleName = "solid" | "dashed" | "dotted";
export type MarkerShape = "arrow_up" | "arrow_down" | "circle" | "square";
export type MarkerPosition = "above" | "below" | "on";

/**
 * What the catalog says about one output (docs/api.md). `color` and `width` name
 * parameters; the colour is read from the instance's params. An M7 output has no `kind`
 * or `style`: it is a solid line.
 */
interface OutputBase {
  name: string; // the key in values, hits and levels (`fill:1` and `level:1` for those kinds)
  label: string;
}
export interface LineOutput extends OutputBase {
  kind?: "line";
  color: string;
  width: string;
  style?: LineStyleName;
}
export interface HistogramOutput extends OutputBase {
  kind: "histogram";
  color: string;
  base: number;
}
export interface AreaOutput extends OutputBase {
  kind: "area";
  color: string;
  width: string | null;
}
export interface FillOutput extends OutputBase {
  kind: "fill";
  between: [string, string]; // the names of two value outputs: upper, lower
  color: string;
  opacity: number;
}
export interface LevelOutput extends OutputBase {
  kind: "level";
  value: number | null;
  input: string | null; // a Float input's name
  color: string;
  width: string | null;
  style: LineStyleName;
}
export interface MarkerOutput extends OutputBase {
  kind: "marker";
  color: string;
  shape: MarkerShape;
  position: MarkerPosition;
  text: string;
}
export interface BackgroundOutput extends OutputBase {
  kind: "background";
  color: string;
  opacity: number;
}
export type IndicatorOutput =
  | LineOutput
  | HistogramOutput
  | AreaOutput
  | FillOutput
  | LevelOutput
  | MarkerOutput
  | BackgroundOutput;

export interface IndicatorDef {
  id: string;
  name: string;
  title: string;
  overlay: boolean;
  params: IndicatorParam[];
  outputs: IndicatorOutput[];
  builtin: boolean;
}

export interface IndicatorCatalog {
  indicators: IndicatorDef[];
  errors: { file: string; error: string }[];
  folder: string;
  /** How your own files are isolated here (CI-1); the notice says when they are not. */
  isolation?: { level: string; minimum_met?: boolean; reason?: string; notice?: string | null; notes?: string[] };
}

export interface IndicatorValues {
  id: string;
  params: IndicatorParams;
  t: number[];
  values: Record<string, (number | null)[]>;
  /** Marker and background outputs: ascending positions in `t` where each is on. */
  hits?: Record<string, number[]>;
  /** Level outputs: the number to draw at, resolved with this request's params. */
  levels?: Record<string, number>;
  /** Your own files only: the save-time checks of this version of the file (`version`: its digest). */
  checks?: { version?: string };
}

export const indicatorsApi = {
  catalog: () => get<IndicatorCatalog>("api/indicators"),
  values: (
    id: string,
    q: { instrument: string; timeframe: string; price: Price; params: IndicatorParams; from?: number; to?: number; limit?: number },
  ) => {
    const query: Record<string, string> = {
      instrument: q.instrument,
      timeframe: q.timeframe,
      price: q.price,
      params: JSON.stringify(q.params),
    };
    if (q.from !== undefined) query.from = String(q.from);
    if (q.to !== undefined) query.to = String(q.to);
    if (q.limit !== undefined) query.limit = String(q.limit);
    return get<IndicatorValues>(`api/indicators/${encodeURIComponent(id)}`, query);
  },
};

// Layouts

export type ChartType = "candles" | "bars" | "line" | "area" | "heikin_ashi";

/** One indicator on the chart, as a layout stores it. */
export interface IndicatorEntry {
  id: string;
  params: IndicatorParams;
  visible: boolean;
  price_labels: boolean;
  legend_values: boolean;
}

export interface LayoutState {
  instrument: string | null;
  timeframe: string;
  chart_type: ChartType;
  price: Price;
  theme: "dark" | "light";
  panel_open: boolean;
  panel_view: "watchlist" | "alerts";
  indicators: IndicatorEntry[];
}

export interface LayoutSummary {
  id: string;
  name: string;
  updated_at: number;
  default: boolean;
}

export interface Layout extends LayoutSummary {
  layout: LayoutState;
}

export const layoutsApi = {
  list: () => get<LayoutSummary[]>("api/layouts"),
  get: (id: string) => get<Layout>(`api/layouts/${id}`),
  create: (body: { name: string; layout: LayoutState; default?: boolean }) => request<Layout>("POST", "api/layouts", { body }),
  update: (id: string, body: { name?: string; layout?: LayoutState; default?: boolean }) =>
    request<Layout>("PUT", `api/layouts/${id}`, { body }),
  remove: (id: string) => request<LayoutSummary>("DELETE", `api/layouts/${id}`),
};
