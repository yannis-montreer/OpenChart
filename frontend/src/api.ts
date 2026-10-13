/** REST client for the OpenChart server (see docs/api.md). */

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

async function get<T>(path: string, params?: Record<string, string | number | undefined>): Promise<T> {
  const url = new URL(path, window.location.href);
  for (const [k, v] of Object.entries(params ?? {})) {
    if (v !== undefined) url.searchParams.set(k, String(v));
  }
  const res = await fetch(url, { headers: { Accept: "application/json" } });
  if (!res.ok) {
    let detail = res.statusText;
    try {
      detail = (await res.json()).detail ?? detail;
    } catch {
      /* not JSON */
    }
    throw new Error(`${res.status} ${detail}`);
  }
  return res.json() as Promise<T>;
}

export const api = {
  meta: () => get<Meta>("api/meta"),
  status: () => get<Status>("api/status"),
  instruments: () => get<Instrument[]>("api/instruments"),
  candles: async (q: CandleQuery): Promise<Bar[]> => {
    const body = await get<{ candles: Bar[] }>("api/candles", { ...q });
    return body.candles;
  },
};
