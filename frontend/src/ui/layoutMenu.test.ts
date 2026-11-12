// @vitest-environment happy-dom
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { layoutsApi, type Layout } from "../api";
import { LayoutMenu } from "./layoutMenu";

const LAYOUT = { id: "L1", name: "Gold H1", updated_at: 1, default: false, layout: { instrument: "XAU_USD", timeframe: "1h" } } as unknown as Layout;
const tick = () => new Promise((r) => setTimeout(r, 0));

async function menu(confirm?: (l: Layout) => Promise<string[] | null>) {
  const apply = vi.fn();
  const m = new LayoutMenu({ state: () => ({}) as never, apply, confirmApply: confirm });
  vi.spyOn(layoutsApi, "list").mockResolvedValue([{ id: "L1", name: "Gold H1", updated_at: 1, default: false }]);
  vi.spyOn(layoutsApi, "get").mockResolvedValue(LAYOUT);
  await m.load();
  document.body.append(m.button);
  m.button.click();
  const item = [...document.querySelectorAll(".menu-item")].find((i) => i.textContent?.includes("Gold H1")) as HTMLElement;
  return {
    m,
    apply,
    load: async () => {
      item.click();
      await tick();
    },
  };
}

beforeEach(() => document.body.replaceChildren());
afterEach(() => vi.restoreAllMocks());

describe("loading a layout that would stop indicator alerts", () => {
  it("applies at once, with no alert named, when nothing asks", async () => {
    const { apply, load, m } = await menu();
    await load();
    expect(apply).toHaveBeenCalledWith(LAYOUT, []);
    expect(m.currentId).toBe("L1");
  });

  it("asks first, and applies with the alerts the person confirmed", async () => {
    const confirm = vi.fn().mockResolvedValue(["a", "b"]);
    const { apply, load } = await menu(confirm);
    await load();
    expect(confirm).toHaveBeenCalledWith(LAYOUT);
    expect(apply).toHaveBeenCalledWith(LAYOUT, ["a", "b"]);
  });

  it("when the person says no, nothing changes: not applied, not the current layout", async () => {
    const { apply, load, m } = await menu(vi.fn().mockResolvedValue(null));
    await load();
    expect(apply).not.toHaveBeenCalled();
    expect(m.currentId).toBeNull();
  });
});
