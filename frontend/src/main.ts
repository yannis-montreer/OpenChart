import "./styles/main.css";
import "./styles/forms.css";
import "./styles/alerts.css";
import "./styles/indicators.css";
import "./styles/editor.css";
import { account, UNAUTHORIZED_EVENT } from "./api";
import { App } from "./app";
import { showLogin, showSetup } from "./ui/auth";
import { h } from "./ui/dom";

async function boot(): Promise<void> {
  const prefsTheme = (() => {
    try {
      return JSON.parse(localStorage.getItem("openchart.prefs.v1") ?? "{}").theme;
    } catch {
      return undefined;
    }
  })();
  document.documentElement.dataset.theme = prefsTheme === "light" ? "light" : "dark";

  let status;
  try {
    status = await account.setupStatus();
  } catch (err) {
    document.body.append(
      h("div", { class: "auth-screen" }, h("div", { class: "auth-card narrow" }, h("h1", {}, "Cannot reach the server"), h("p", { class: "lead" }, String(err)))),
    );
    window.setTimeout(() => location.reload(), 10000);
    return;
  }
  if (status.setup_required) return showSetup(status);
  if (!status.authenticated) return showLogin();

  window.addEventListener(UNAUTHORIZED_EVENT, () => showLogin("Your session has ended. Please log in again."));
  const app = new App(document.getElementById("app")!);
  // Handy in the browser console when something looks wrong.
  (window as unknown as { openchart: App }).openchart = app;
  await app.start();
}

void boot();
