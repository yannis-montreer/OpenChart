/** Settings dialog (SPEC section 8.3). Each section saves on its own. */

import { account, api, type Settings } from "../api";
import { removedInstruments, removedLine, visibleInstruments } from "../instruments";
import { deleteInstrumentDialog } from "./deleteInstrument";
import { isDownloading, lineText } from "./download";
import { field, primary, resultLine, secondary, secretInput, segmented, textInput, toggleSwitch, withBusy } from "./forms";
import { button, h, icon, type IconName } from "./dom";
import {
  DEFAULT_M1_DEPTH,
  DEFAULT_TICK_RETENTION,
  M1_DEPTHS,
  M1_HELP,
  TICK_HELP,
  TICK_RETENTION,
  depthLabel,
  depthLine,
  depthSaved,
  tickLabel,
} from "./retention";

/** A select of `choices`, labelled by `label`, with `current` selected. */
function choiceSelect(choices: readonly string[], current: string, label: (c: string) => string): HTMLSelectElement {
  const select = h("select", { class: "input" }) as HTMLSelectElement;
  select.replaceChildren(...choices.map((c) => h("option", { value: c, selected: c === current }, label(c))));
  return select;
}

export type Section = "security" | "broker" | "telegram" | "instruments" | "data" | "network";

const SECTIONS: [Section, string, IconName][] = [
  ["security", "Password & login", "lock"],
  ["broker", "Broker", "bank"],
  ["telegram", "Telegram alerts", "send"],
  ["instruments", "Instruments", "list"],
  ["data", "Data & time", "database"],
  ["network", "Network", "globe"],
];

const TIMEZONES = [
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

const errorText = (err: unknown) => (err instanceof Error ? err.message : String(err));

export class SettingsDialog {
  private backdrop?: HTMLElement;
  private body!: HTMLElement;
  private nav!: HTMLElement;
  private settings?: Settings;
  /** Set when instruments changed, so the app can refresh its lists on close. */
  instrumentsChanged = false;
  onClose: (changedInstruments: boolean) => void = () => {};

  async open(section: Section = "security"): Promise<void> {
    if (this.backdrop) return this.show(section);
    this.nav = h("nav", { class: "modal-nav", "aria-label": "Settings sections" });
    this.body = h("div", { class: "modal-body" });
    const close = button({ icon: "x", title: "Close (Esc)", onClick: () => this.close() });
    const modal = h(
      "div",
      { class: "modal", role: "dialog", "aria-modal": "true", "aria-label": "Settings" },
      h("div", { class: "modal-head" }, h("h2", {}, "Settings"), close),
      this.nav,
      this.body,
    );
    this.backdrop = h("div", { class: "modal-backdrop" }, modal);
    this.backdrop.addEventListener("pointerdown", (ev) => {
      if (ev.target === this.backdrop) this.close();
    });
    document.addEventListener("keydown", this.onKey, true);
    document.body.append(this.backdrop);
    try {
      this.settings = await account.settings();
    } catch (err) {
      this.body.replaceChildren(h("div", { class: "notice bad" }, errorText(err)));
      return;
    }
    this.show(section);
  }

  close(): void {
    if (!this.backdrop) return;
    this.backdrop.remove();
    this.backdrop = undefined;
    document.removeEventListener("keydown", this.onKey, true);
    this.onClose(this.instrumentsChanged);
    this.instrumentsChanged = false;
  }

  private onKey = (ev: KeyboardEvent) => {
    if (ev.key === "Escape" && !document.querySelector(".menu")) {
      ev.stopPropagation();
      this.close();
    }
  };

  private show(section: Section): void {
    this.nav.replaceChildren(
      ...SECTIONS.map(([id, label, ic]) => {
        const b = h("button", { class: `btn${id === section ? " active" : ""}`, type: "button" }, icon(ic, 17), label);
        b.addEventListener("click", () => this.show(id));
        return b;
      }),
    );
    const render: Record<Section, () => Node[]> = {
      security: () => this.security(),
      broker: () => this.broker(),
      telegram: () => this.telegram(),
      instruments: () => this.instruments(),
      data: () => this.data(),
      network: () => this.network(),
    };
    this.body.replaceChildren(...render[section]());
    this.body.scrollTop = 0;
  }

  private save = async (body: Record<string, unknown>) => {
    this.settings = await account.saveSettings(body);
    return this.settings;
  };

  // Sections

  private security(): Node[] {
    const current = secretInput({ autocomplete: "current-password" });
    const next = secretInput({ autocomplete: "new-password" });
    const again = secretInput({ autocomplete: "new-password" });
    const result = resultLine();
    const change = primary("Change password");
    change.addEventListener("click", () =>
      withBusy(change, async () => {
        if (next.input.value !== again.input.value) return result.fail("The new passwords differ.");
        result.busy();
        try {
          await account.changePassword(current.input.value, next.input.value);
          result.ok("Password changed. Other browsers and devices have been logged out.");
          current.input.value = next.input.value = again.input.value = "";
        } catch (err) {
          result.fail(errorText(err));
        }
      }),
    );
    const logout = secondary("Log out of this browser", async () => {
      await account.logout().catch(() => undefined);
      location.reload();
    }, "logout");
    return [
      h("h3", {}, "Password"),
      field("Current password", current.el),
      h("div", { class: "grid-2" }, field("New password", next.el, "At least 8 characters."), field("Repeat new password", again.el)),
      h("div", { class: "row" }, change),
      result.el,
      h("hr", { class: "section-sep" }),
      h("h3", {}, "Session"),
      h("p", { class: "prose" }, "A login lasts 30 days on each browser. Forgot the password? On the server, run ", h("code", {}, "openchart reset-password"), "."),
      h("div", { class: "row" }, logout),
    ];
  }

  private broker(): Node[] {
    const s = this.settings!;
    const env = segmented<"practice" | "live">([["practice", "Practice"], ["live", "Live"]], s.oanda.environment);
    const accountId = textInput({ value: s.oanda.account_id });
    const token = secretInput({
      placeholder: s.oanda.token.set
        ? `Saved (${s.oanda.token.masked})${s.oanda.token.source === "environment" ? ", from .env" : ""}. Paste a new one to replace it.`
        : "Paste your personal access token",
    });
    const result = resultLine();
    const values = () => ({ environment: env.value(), account_id: accountId.value.trim(), token: token.input.value.trim() });
    const test = secondary("Test connection", undefined, "plug");
    test.addEventListener("click", () =>
      withBusy(test, async () => {
        result.busy("Contacting OANDA…");
        try {
          const r = await account.testOanda(values());
          (r.ok ? result.ok : result.fail)(r.message);
        } catch (err) {
          result.fail(errorText(err));
        }
      }),
    );
    const save = primary("Save");
    save.addEventListener("click", () =>
      withBusy(save, async () => {
        result.busy("Checking the credentials, then saving…");
        const v = values();
        try {
          await this.save({ oanda_environment: v.environment, oanda_account_id: v.account_id, oanda_token: v.token });
          token.input.value = "";
          result.ok("Saved. The price feed reconnected with the new settings.");
        } catch (err) {
          result.fail(errorText(err));
        }
      }),
    );
    return [
      h("h3", {}, "OANDA"),
      h("p", { class: "prose" }, "Credentials are checked with OANDA before they are saved. The token is stored in the system's credential store and never shown again."),
      field("Account type", env.el),
      field("Account ID", accountId),
      field("API token", token.el, "OANDA portal: Tools, API, Manage API Access."),
      h("div", { class: "row" }, test, save),
      result.el,
    ];
  }

  private telegram(): Node[] {
    const s = this.settings!;
    const token = secretInput({
      placeholder: s.telegram.token.set ? `Saved (${s.telegram.token.masked}). Paste a new one to replace it.` : "123456789:AA…",
    });
    const chat = textInput({ value: s.telegram.chat_id, placeholder: "e.g. 123456789" });
    const result = resultLine();
    const test = secondary("Send test message", undefined, "send");
    test.addEventListener("click", () =>
      withBusy(test, async () => {
        result.busy("Sending…");
        try {
          const r = await account.testTelegram({ token: token.input.value.trim(), chat_id: chat.value.trim() });
          (r.ok ? result.ok : result.fail)(r.message);
        } catch (err) {
          result.fail(errorText(err));
        }
      }),
    );
    const save = primary("Save");
    save.addEventListener("click", () =>
      withBusy(save, async () => {
        result.busy();
        try {
          const out = await this.save({ telegram_token: token.input.value.trim(), telegram_chat_id: chat.value.trim() });
          token.input.value = "";
          token.input.placeholder = `Saved (${out.telegram.token.masked}). Paste a new one to replace it.`;
          result.ok("Saved.");
        } catch (err) {
          result.fail(errorText(err));
        }
      }),
    );
    const remove = secondary("Remove Telegram", undefined, "trash");
    remove.classList.add("btn-danger");
    remove.addEventListener("click", () =>
      withBusy(remove, async () => {
        try {
          await this.save({ clear_telegram: true });
          token.input.value = chat.value = "";
          token.input.placeholder = "123456789:AA…";
          result.ok("Telegram removed.");
        } catch (err) {
          result.fail(errorText(err));
        }
      }),
    );
    return [
      h("h3", {}, "Telegram"),
      h(
        "p",
        { class: "prose" },
        "Alerts (coming in M6) are sent as Telegram messages. Create a bot with @BotFather and copy its token, send the bot a message, then get your chat ID from @userinfobot.",
      ),
      h("div", { class: "grid-2" }, field("Bot token", token.el), field("Chat ID", chat)),
      h("div", { class: "row" }, test, save, s.telegram.token.set ? remove : null),
      result.el,
    ];
  }

  private instruments(): Node[] {
    const list = h("div", { class: "inst-list" });
    const removed = h("div", { class: "inst-list" });
    const results = h("div", { class: "inst-list" });
    const result = resultLine();
    const search = textInput({ placeholder: "Search, e.g. EUR, gold, NAS…" });
    const newDepth = choiceSelect(M1_DEPTHS, DEFAULT_M1_DEPTH, depthLabel);

    let anyDownloading = false;
    const renderMine = async () => {
      const [all, status] = await Promise.all([api.instruments(), api.status().catch(() => undefined)]);
      const rows = visibleInstruments(all);
      anyDownloading = rows.some((i) => isDownloading(status?.instruments?.[i.instrument]?.history));
      const downloading = (name: string) => {
        const history = status?.instruments?.[name]?.history;
        return isDownloading(history) ? h("small", { class: "dl-line-small" }, lineText(history)) : null;
      };
      list.replaceChildren(
        ...rows.map((i) => {
          const remove = h("button", { class: "btn btn-danger", type: "button" }, "Remove");
          remove.addEventListener("click", () =>
            withBusy(remove, async () => {
              try {
                await account.removeInstrument(i.instrument);
                this.instrumentsChanged = true;
                result.ok(`${i.instrument} removed. Its stored history is kept.`);
                await renderMine();
                await runSearch();
              } catch (err) {
                result.fail(errorText(err));
              }
            }),
          );
          const depth = choiceSelect(M1_DEPTHS, i.m1_depth ?? DEFAULT_M1_DEPTH, depthLabel);
          depth.title = M1_HELP;
          depth.setAttribute("aria-label", `Minute history of ${i.instrument}`);
          depth.addEventListener("change", async () => {
            depth.disabled = true;
            try {
              const saved = await account.setM1Depth(i.instrument, depth.value);
              result.ok(depthSaved(i.instrument, saved.m1_depth, saved.downloading));
              await renderMine();
            } catch (err) {
              result.fail(errorText(err));
            } finally {
              depth.disabled = false;
            }
          });
          const reach = i.m1_from !== undefined ? h("small", {}, depthLine(i.m1_depth ?? DEFAULT_M1_DEPTH, i.m1_from)) : null;
          return h(
            "div",
            { class: "inst-row" },
            h("div", { class: "name" }, h("b", {}, i.instrument), h("small", {}, i.display_name), reach, downloading(i.instrument)),
            depth,
            rows.length > 1 ? remove : null,
          );
        }),
      );
      const gone = removedInstruments(all);
      removed.replaceChildren(
        ...(gone.length ? [h("hr", { class: "section-sep" }), h("h3", {}, "Removed")] : []),
        ...gone.map((i) => {
          const back = h("button", { class: "btn btn-secondary", type: "button" }, "Add back");
          back.addEventListener("click", () =>
            withBusy(back, async () => {
              result.busy(`Adding ${i.instrument} back…`);
              try {
                await account.addInstrument(i.instrument);
                this.instrumentsChanged = true;
                result.ok(`${i.instrument} is back. Its stored history was kept; the gap since it was removed downloads in the background.`);
                await renderMine();
                await runSearch();
              } catch (err) {
                result.fail(errorText(err));
              }
            }),
          );
          const erase = h("button", { class: "btn btn-danger", type: "button" }, "Delete data…");
          erase.addEventListener("click", () =>
            withBusy(erase, async () => {
              const done = await deleteInstrumentDialog(i.instrument);
              if (!done) return;
              this.instrumentsChanged = true;
              result.ok(done.text);
              await renderMine();
              await runSearch();
            }),
          );
          return h(
            "div",
            { class: "inst-row" },
            h("div", { class: "name" }, h("b", {}, i.instrument), h("small", {}, removedLine(i))),
            back,
            erase,
          );
        }),
      );
    };

    let timer = 0;
    const runSearch = async () => {
      const q = search.value.trim();
      if (!q) {
        results.replaceChildren();
        return;
      }
      try {
        const found = (await account.available(q)).slice(0, 30);
        results.replaceChildren(
          ...(found.length
            ? found.map((i) => {
                const add = h("button", { class: "btn btn-secondary", type: "button", disabled: i.enabled }, i.enabled ? "Added" : "Add");
                add.addEventListener("click", () =>
                  withBusy(add, async () => {
                    result.busy(`Adding ${i.instrument}…`);
                    try {
                      await account.addInstrument(i.instrument, newDepth.value);
                      this.instrumentsChanged = true;
                      result.ok(`${i.instrument} added. Its history downloads in the background, newest first: hourly candles within seconds, then minute candles (a few minutes).`);
                      await renderMine();
                      await runSearch();
                    } catch (err) {
                      result.fail(errorText(err));
                    }
                  }),
                );
                return h("div", { class: "inst-row" }, h("div", { class: "name" }, h("b", {}, i.instrument), h("small", {}, `${i.display_name} · ${i.kind.toLowerCase()}`)), add);
              })
            : [h("div", { class: "inst-row muted" }, "Nothing found on this account.")]),
        );
      } catch (err) {
        result.fail(errorText(err));
      }
    };
    search.addEventListener("input", () => {
      window.clearTimeout(timer);
      timer = window.setTimeout(() => void runSearch(), 250);
    });
    void renderMine();
    // While the section is open, keep download progress current.
    const refresh = window.setInterval(() => {
      if (!list.isConnected) window.clearInterval(refresh);
      else if (anyDownloading) void renderMine().catch(() => undefined);
    }, 3000);
    return [
      h("h3", {}, "Your instruments"),
      list,
      result.el,
      removed,
      h("hr", { class: "section-sep" }),
      h("h3", {}, "Add an instrument"),
      field("Search your OANDA account", search),
      field("Minute history", newDepth, M1_HELP),
      results,
    ];
  }

  private data(): Node[] {
    const s = this.settings!;
    const keep = s.tick_retention ?? (s.store_ticks ? DEFAULT_TICK_RETENTION : "off");
    const ticks = choiceSelect(s.tick_retention_choices ?? TICK_RETENTION, keep, tickLabel);
    const tz = h("select", { class: "input" }) as HTMLSelectElement;
    const local = Intl.DateTimeFormat().resolvedOptions().timeZone;
    const zones = [...new Set([s.timezone || local, local, ...TIMEZONES])];
    tz.replaceChildren(...zones.map((z) => h("option", { value: z, selected: z === (s.timezone || local) }, z.replace(/_/g, " "))));
    const result = resultLine();
    const save = primary("Save");
    save.addEventListener("click", () =>
      withBusy(save, async () => {
        result.busy();
        try {
          await this.save({ tick_retention: ticks.value, timezone: tz.value });
          result.ok("Saved.");
        } catch (err) {
          result.fail(errorText(err));
        }
      }),
    );
    return [
      h("h3", {}, "Data"),
      field("Keep raw ticks", ticks, TICK_HELP),
      h("hr", { class: "section-sep" }),
      h("h3", {}, "Time"),
      field("Timezone for alerts", tz, "Used for times in Telegram messages. Each browser picks its own chart timezone at the bottom right."),
      h("div", { class: "row" }, save),
      result.el,
    ];
  }

  private network(): Node[] {
    const s = this.settings!;
    const lan = toggleSwitch(
      "Allow other devices to connect",
      s.listen_on_network,
      "Lets your phone or another PC open OpenChart, for example over Tailscale. Takes effect after the server restarts.",
    );
    const result = resultLine();
    const save = primary("Save");
    save.addEventListener("click", () =>
      withBusy(save, async () => {
        result.busy();
        try {
          const out = await this.save({ listen_on_network: lan.input.checked });
          result.ok(out.restart_required ? "Saved. Restart the server to apply it." : "Saved.");
        } catch (err) {
          result.fail(errorText(err));
        }
      }),
    );
    return [
      h("h3", {}, "Access from other devices"),
      lan.el,
      h(
        "div",
        { class: "notice" },
        "Use a private network such as Tailscale, and never forward this port on your router. The password protects the app, but it should not be your only lock.",
      ),
      h("div", { class: "row" }, save),
      result.el,
      h("hr", { class: "section-sep" }),
      h("h3", {}, "Secret storage"),
      h(
        "p",
        { class: "prose" },
        s.secret_store
          ? `Tokens are kept in: ${s.secret_store === "WinVaultKeyring" ? "Windows Credential Manager" : s.secret_store}.`
          : s.secret_store_error ?? "No secret storage available.",
      ),
    ];
  }
}
