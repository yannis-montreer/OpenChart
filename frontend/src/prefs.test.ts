import { describe, expect, it } from "vitest";

import { DEFAULT_PREFS, dropLegacyIndicators, loadPrefs, readLegacyIndicators, savePrefs } from "./prefs";

const KEY = "openchart.prefs.v1";

function memory(initial?: unknown): Pick<Storage, "getItem" | "setItem"> {
  const data = new Map<string, string>();
  if (initial !== undefined) data.set(KEY, JSON.stringify(initial));
  return { getItem: (k) => data.get(k) ?? null, setItem: (k, v) => void data.set(k, v) };
}

const stored = (m: ReturnType<typeof memory>) => JSON.parse(m.getItem(KEY) ?? "null");
const OLD = [{ id: "ema", params: { length: 9 }, visible: true, price_labels: true, legend_values: true }];

describe("the old list in the browser's preferences", () => {
  it("is read from what is stored", () => {
    expect(readLegacyIndicators(memory({ theme: "light", indicators: OLD }))).toEqual(OLD);
  });

  it("is nothing when there is none, or storage is empty or unreadable", () => {
    expect(readLegacyIndicators(memory({ theme: "light" }))).toEqual([]);
    expect(readLegacyIndicators(memory())).toEqual([]);
    expect(readLegacyIndicators({ getItem: () => "{not json", setItem: () => undefined })).toEqual([]);
    expect(readLegacyIndicators({ getItem: () => { throw new Error("blocked"); }, setItem: () => undefined })).toEqual([]);
  });

  it("is not part of the preferences the page works with", () => {
    const prefs = loadPrefs(memory({ theme: "light", indicators: OLD }));
    expect(prefs.theme).toBe("light");
    expect("indicators" in prefs).toBe(false);
    expect("indicators" in DEFAULT_PREFS).toBe(false);
  });

  it("survives saving other preferences until it is dropped", () => {
    const m = memory({ theme: "dark", indicators: OLD });
    const prefs = loadPrefs(m);
    prefs.theme = "light";
    savePrefs(prefs, m);
    expect(stored(m).theme).toBe("light");
    expect(stored(m).indicators).toEqual(OLD);
  });

  it("goes when dropped, and the other preferences stay", () => {
    const m = memory({ theme: "light", timeframe: "1h", indicators: OLD });
    dropLegacyIndicators(m);
    expect(stored(m)).toEqual({ theme: "light", timeframe: "1h" });
    savePrefs(loadPrefs(m), m);
    expect("indicators" in stored(m)).toBe(false);
  });

  it("is nothing when what is stored under it is not a list", () => {
    expect(readLegacyIndicators(memory({ indicators: {} }))).toEqual([]);
    expect(readLegacyIndicators(memory({ indicators: "ema" }))).toEqual([]);
  });

  it("dropping when there is none writes nothing", () => {
    let writes = 0;
    const m = { getItem: () => JSON.stringify({ theme: "light" }), setItem: () => void writes++ };
    dropLegacyIndicators(m);
    expect(writes).toBe(0);
  });

  it("dropping with nothing stored does nothing", () => {
    const m = memory();
    dropLegacyIndicators(m);
    expect(m.getItem(KEY)).toBeNull();
  });
});
