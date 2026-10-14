/** Login screen and first-run setup wizard. Both reload the page when they succeed. */

import { account, ApiError, type SetupStatus } from "../api";
import {
  field,
  primary,
  resultLine,
  secondary,
  secretInput,
  segmented,
  textInput,
  withBusy,
} from "./forms";
import { h, icon } from "./dom";

function brand(): HTMLElement {
  return h("div", { class: "brand" }, h("span", { class: "brand-mark" }, icon("candles", 18)), "OpenChart");
}

function screen(card: HTMLElement): HTMLElement {
  const el = h("div", { class: "auth-screen" }, card);
  document.body.append(el);
  return el;
}

const errorText = (err: unknown) => (err instanceof Error ? err.message : String(err));

export function showLogin(reason?: string): void {
  if (document.querySelector(".auth-screen")) return;
  const pw = secretInput({ autocomplete: "current-password", name: "password" });
  const result = resultLine();
  const submit = primary("Log in");
  const form = h(
    "form",
    { class: "auth-card narrow" },
    brand(),
    h("h1", {}, "Log in"),
    reason ? h("p", { class: "lead" }, reason) : null,
    field("Password", pw.el),
    submit,
    result.el,
    h("p", { class: "prose" }, "Forgot it? On the server, run ", h("code", {}, "openchart reset-password"), "."),
  );
  submit.type = "submit";
  form.addEventListener("submit", async (ev) => {
    ev.preventDefault();
    if (!pw.input.value) return;
    await withBusy(submit, async () => {
      result.busy("Checking…");
      try {
        await account.login(pw.input.value);
        location.reload();
      } catch (err) {
        result.fail(errorText(err));
        pw.input.select();
      }
    });
  });
  screen(form);
  pw.input.focus();
}

export async function showSetup(status: SetupStatus): Promise<void> {
  const codeInput = textInput({ placeholder: "6 characters", autocomplete: "one-time-code" });
  const code = () => codeInput.value.trim().toUpperCase() || undefined;

  // 1. Password
  const pw1 = secretInput({ autocomplete: "new-password", name: "new-password" });
  const pw2 = secretInput({ autocomplete: "new-password" });

  // 2. Broker
  const env = segmented<"practice" | "live">([["practice", "Practice"], ["live", "Live"]], "practice");
  const accountId = textInput({ placeholder: "101-004-1234567-001" });
  const token = secretInput({ placeholder: "Paste your personal access token" });
  const oandaResult = resultLine();
  const tokenHint = h(
    "span",
    {},
    "In the OANDA account portal: Tools, API, Manage API Access. Practice and live accounts use different tokens.",
  );
  const testOanda = secondary("Test connection", undefined, "plug");

  // 3. Telegram
  const tgToken = secretInput({ placeholder: "123456789:AA…" });
  const tgChat = textInput({ placeholder: "e.g. 123456789" });
  const tgResult = resultLine();
  const testTg = secondary("Send test message", undefined, "send");

  // 4. Instrument
  const instrument = textInput({ value: "XAU_USD", placeholder: "XAU_USD" });

  const finish = primary("Finish setup");
  const finishResult = resultLine();

  const prefill = async () => {
    try {
      const d = await account.setupDefaults(code());
      env.set(d.oanda.environment);
      if (d.oanda.account_id) accountId.value = d.oanda.account_id;
      if (d.oanda.token.set) {
        token.input.placeholder = `Leave empty to keep the token from your .env file (${d.oanda.token.masked})`;
      }
    } catch {
      /* nothing known yet, or the setup code is still missing */
    }
  };

  testOanda.addEventListener("click", () =>
    withBusy(testOanda, async () => {
      oandaResult.busy("Contacting OANDA…");
      try {
        const r = await account.testOanda(
          { environment: env.value(), account_id: accountId.value.trim(), token: token.input.value.trim() },
          code(),
        );
        (r.ok ? oandaResult.ok : oandaResult.fail)(r.message);
      } catch (err) {
        oandaResult.fail(err instanceof ApiError && err.status === 401 ? "Enter the setup code first." : errorText(err));
      }
    }),
  );
  testTg.addEventListener("click", () =>
    withBusy(testTg, async () => {
      tgResult.busy("Sending…");
      try {
        const r = await account.testTelegram({ token: tgToken.input.value.trim(), chat_id: tgChat.value.trim() }, code());
        (r.ok ? tgResult.ok : tgResult.fail)(r.message);
      } catch (err) {
        tgResult.fail(errorText(err));
      }
    }),
  );
  finish.addEventListener("click", () =>
    withBusy(finish, async () => {
      if (pw1.input.value !== pw2.input.value) {
        finishResult.fail("The two passwords differ.");
        return;
      }
      finishResult.busy("Checking your OANDA account and saving…");
      try {
        await account.setup({
          password: pw1.input.value,
          oanda_environment: env.value(),
          oanda_account_id: accountId.value.trim(),
          oanda_token: token.input.value.trim(),
          telegram_token: tgToken.input.value.trim(),
          telegram_chat_id: tgChat.value.trim(),
          instrument: instrument.value.trim(),
          setup_code: code() ?? "",
        });
        finishResult.ok("Done. Downloading price history…");
        location.reload();
      } catch (err) {
        finishResult.fail(errorText(err));
      }
    }),
  );

  const step = (n: number, title: string, optional: boolean, ...body: (Node | null)[]) =>
    h(
      "section",
      { class: "step" },
      h("div", { class: "step-title" }, h("span", { class: "step-num" }, String(n)), title, optional ? h("span", { class: "optional" }, "optional") : null),
      ...body,
    );

  const card = h(
    "div",
    { class: "auth-card" },
    brand(),
    h("h1", {}, "Set up OpenChart"),
    h(
      "p",
      { class: "lead" },
      "This takes about two minutes. Everything stays on this server: " +
        (status.secret_store?.startsWith("encrypted file")
          ? "tokens are encrypted in its data folder, with a key kept apart."
          : "tokens go to the system's credential store, never into files."),
    ),
    status.secret_store_error ? h("div", { class: "notice bad" }, status.secret_store_error) : null,
    status.setup_code_required
      ? h(
          "div",
          { class: "notice" },
          field("Setup code", codeInput, "You are not on the server computer, so enter the setup code shown in the server's console window."),
        )
      : null,
    step(1, "Choose a password", false, h("div", { class: "grid-2" }, field("Password", pw1.el, "At least 8 characters."), field("Repeat it", pw2.el))),
    step(
      2,
      "Connect your OANDA account",
      false,
      field("Account type", env.el),
      field("Account ID", accountId, "Shown in the OANDA portal next to your account, e.g. 101-004-1234567-001."),
      field("API token", token.el, tokenHint),
      h("div", { class: "row" }, testOanda),
      oandaResult.el,
    ),
    step(
      3,
      "Alerts on your phone (Telegram)",
      true,
      h(
        "p",
        { class: "prose" },
        "In Telegram, talk to @BotFather to create a bot and copy its token. Then send your bot any message, and get your chat ID from @userinfobot. You can also do this later in Settings.",
      ),
      h("div", { class: "grid-2" }, field("Bot token", tgToken.el), field("Chat ID", tgChat)),
      h("div", { class: "row" }, testTg),
      tgResult.el,
    ),
    step(4, "First instrument", false, field("Instrument", instrument, "OANDA names use an underscore: XAU_USD for gold, EUR_USD for the euro.")),
    finish,
    finishResult.el,
  );
  codeInput.addEventListener("change", () => void prefill());
  screen(card);
  void prefill();
  (status.setup_code_required ? codeInput : pw1.input).focus();
}
