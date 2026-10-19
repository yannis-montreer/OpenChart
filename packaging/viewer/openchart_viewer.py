"""OpenChart Viewer: a window on an OpenChart server (SPEC section 15).

A thin pywebview window around the web page the server serves; it holds no data. On the
first launch it asks for the server's address (for example http://my-pc:8000 over
Tailscale) and remembers it in %APPDATA%\\OpenChart Viewer\\viewer.json; the OpenChart menu
changes it later. The login is kept between launches like in a browser.

It needs the Microsoft Edge WebView2 Runtime (part of Windows 11 and of up-to-date
Windows 10). Without it, pywebview would fall back to Internet Explorer's engine, which
cannot run the page; the viewer says so instead and offers the download page, or opening
OpenChart in the default browser.
"""

from __future__ import annotations

import ctypes
import html
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from pathlib import Path

APP = "OpenChart Viewer"
VERSION = "0.2.0"
DEFAULT_SERVER = "http://localhost:8000"
WEBVIEW2_DOWNLOAD = "https://developer.microsoft.com/microsoft-edge/webview2/"
WEBVIEW2_RUNTIME_KEY = "{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}"


def app_dir() -> Path:
    """Settings and the browser profile (login cookie). OPENCHART_VIEWER_HOME moves them,
    for a portable copy or tests."""
    override = os.environ.get("OPENCHART_VIEWER_HOME")
    if override:
        return Path(override)
    return Path(os.environ.get("APPDATA") or str(Path.home())) / APP


def resource(name: str) -> Path:
    """A file bundled with the program (LICENSE, NOTICE, ...)."""
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parents[2]))
    return base / name


# Settings


def load_server() -> str | None:
    try:
        data = json.loads((app_dir() / "viewer.json").read_text(encoding="utf-8"))
        server = data.get("server")
        return server if isinstance(server, str) and server else None
    except (OSError, ValueError):
        return None


def save_server(server: str) -> None:
    folder = app_dir()
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "viewer.json").write_text(json.dumps({"server": server}, indent=2), encoding="utf-8")


def normalize(address: str) -> str:
    """'my-pc' -> 'http://my-pc:8000'; keeps a scheme, port and path that are given."""
    address = address.strip()
    if not address:
        raise ValueError("Enter the server's address.")
    if "://" not in address:
        address = "http://" + address
    parts = urllib.parse.urlsplit(address)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise ValueError("The address looks like http://my-pc:8000")
    netloc = parts.netloc if parts.port or parts.scheme == "https" else f"{parts.netloc}:8000"
    return urllib.parse.urlunsplit((parts.scheme, netloc, parts.path.rstrip("/"), "", ""))


def check(server: str, timeout: float = 5.0) -> str | None:
    """None if an OpenChart server answers at `server`, else why not."""
    not_ours = f"{server} answered, but it is not an OpenChart server."
    try:
        with urllib.request.urlopen(f"{server}/api/health", timeout=timeout) as r:
            raw = r.read().decode("utf-8", "replace")
        try:
            body = json.loads(raw)
        except ValueError:
            return not_ours
        return None if isinstance(body, dict) and body.get("status") == "ok" else not_ours
    except urllib.error.HTTPError as exc:
        return f"{server} answered with an error ({exc.code}); is it an OpenChart server?"
    except (urllib.error.URLError, OSError, ValueError) as exc:
        reason = getattr(exc, "reason", exc)
        return f"Cannot reach {server}: {reason}. Is the server running, and Tailscale connected?"


# WebView2


def webview2_installed() -> bool:
    if sys.platform != "win32":
        return True
    import winreg

    paths = [
        (
            winreg.HKEY_LOCAL_MACHINE,
            rf"SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients\{WEBVIEW2_RUNTIME_KEY}",
        ),
        (
            winreg.HKEY_LOCAL_MACHINE,
            rf"SOFTWARE\Microsoft\EdgeUpdate\Clients\{WEBVIEW2_RUNTIME_KEY}",
        ),
        (
            winreg.HKEY_CURRENT_USER,
            rf"SOFTWARE\Microsoft\EdgeUpdate\Clients\{WEBVIEW2_RUNTIME_KEY}",
        ),
    ]
    for hive, path in paths:
        try:
            with winreg.OpenKey(hive, path) as key:
                version, _ = winreg.QueryValueEx(key, "pv")
                if version and version != "0.0.0.0":
                    return True
        except OSError:
            continue
    return False


def message_box(text: str, title: str = APP, flags: int = 0x40) -> int:
    """A native Windows message box (0x40: information; 0x30: warning; 0x3: yes/no/cancel)."""
    if sys.platform == "win32":
        return ctypes.windll.user32.MessageBoxW(None, text, title, flags)
    print(f"{title}: {text}", file=sys.stderr)
    return 0


def no_webview2(server: str | None) -> None:
    where = server or DEFAULT_SERVER
    answer = message_box(
        "OpenChart Viewer needs the Microsoft Edge WebView2 Runtime, which is not "
        "installed on this PC (it comes with Windows 11 and up-to-date Windows 10).\n\n"
        "Yes: open Microsoft's download page (choose the Evergreen Bootstrapper), then start "
        "the viewer again.\n"
        f"No: open OpenChart in your web browser instead ({where}).\n"
        "Cancel: close.",
        f"{APP}: WebView2 is missing",
        0x3 | 0x30,
    )
    if answer == 6:  # Yes
        webbrowser.open(WEBVIEW2_DOWNLOAD)
    elif answer == 7:  # No
        webbrowser.open(where)


# The address page (first launch, Change server, server unreachable)

ADDRESS_PAGE = """<!doctype html><html><head><meta charset="utf-8"><title>OpenChart</title>
<style>
  :root { color-scheme: dark; }
  body { margin: 0; height: 100vh; display: grid; place-items: center; background: #0f1218;
         color: #d6dae3; font: 14px system-ui, "Segoe UI", sans-serif; }
  .card { width: min(440px, calc(100vw - 32px)); background: #171b23; border: 1px solid #2a303c;
          border-radius: 12px; padding: 28px; box-shadow: 0 10px 40px rgba(0,0,0,.4); }
  h1 { font-size: 20px; margin: 0 0 6px; color: #f1f3f7; }
  p { color: #9aa3b2; line-height: 1.5; margin: 0 0 18px; }
  label { display: block; font-weight: 600; margin-bottom: 6px; color: #f1f3f7; }
  input { width: 100%; box-sizing: border-box; height: 40px; padding: 0 12px; border-radius: 8px;
          border: 1px solid #2f3643; background: #0f1218; color: #f1f3f7; font: inherit; }
  input:focus { outline: 2px solid #4d8dff; border-color: transparent; }
  button { margin-top: 16px; width: 100%; height: 40px; border: 0; border-radius: 8px;
           background: #4d8dff; color: white; font: 600 14px inherit; cursor: pointer; }
  button:disabled { opacity: .6; cursor: default; }
  .error { color: #f3a14b; margin-top: 14px; min-height: 1.4em; line-height: 1.4; }
  .hint { color: #7d8697; font-size: 12.5px; margin-top: 16px; }
</style></head><body>
<form class="card" id="f">
  <h1>Connect to your OpenChart server</h1>
  <p>The address of the PC running the OpenChart server. Over Tailscale that is its machine
  name, for example <b>http://my-pc:8000</b>; on that PC itself, http://localhost:8000.</p>
  <label for="a">Server address</label>
  <input id="a" value="__VALUE__" spellcheck="false" autofocus>
  <button id="b" type="submit">Connect</button>
  <div class="error" id="e">__ERROR__</div>
  <div class="hint">Remembered for the next launch. Change it later in the OpenChart menu.</div>
</form>
<script>
  const f = document.getElementById("f"), a = document.getElementById("a"),
        b = document.getElementById("b"), e = document.getElementById("e");
  a.select();
  f.addEventListener("submit", async (ev) => {
    ev.preventDefault();
    b.disabled = true; b.textContent = "Connecting..."; e.textContent = "";
    const err = await window.pywebview.api.connect(a.value);
    if (err) { e.textContent = err; b.disabled = false; b.textContent = "Connect"; a.focus(); }
  });
</script></body></html>"""


def address_page(value: str, error: str = "") -> str:
    return ADDRESS_PAGE.replace("__VALUE__", html.escape(value, quote=True)).replace(
        "__ERROR__", html.escape(error)
    )


class Api:
    """Called from the address page."""

    def __init__(self) -> None:
        self._window = None  # private: pywebview exposes public attributes to JavaScript

    def connect(self, address: str) -> str | None:
        try:
            server = normalize(address)
        except ValueError as exc:
            return str(exc)
        problem = check(server)
        if problem:
            return problem
        save_server(server)
        self._window.load_url(server + "/")
        self._window.title = f"OpenChart ({urllib.parse.urlsplit(server).hostname})"
        return None


def main() -> int:
    server = load_server()
    if not webview2_installed():
        no_webview2(server)
        return 1

    import webview
    from webview.menu import Menu, MenuAction, MenuSeparator

    api = Api()
    problem = check(server) if server else None
    if server and not problem:
        start = {"url": server + "/"}
        title = f"OpenChart ({urllib.parse.urlsplit(server).hostname})"
    else:
        start = {"html": address_page(server or DEFAULT_SERVER, problem or "")}
        title = "OpenChart"
    window = webview.create_window(
        title,
        js_api=api,
        width=1440,
        height=900,
        min_size=(640, 480),
        background_color="#0f1218",
        **start,
    )
    api._window = window

    def change_server() -> None:
        window.load_html(address_page(load_server() or DEFAULT_SERVER))
        window.title = "OpenChart"

    def reload_page() -> None:
        current = load_server()
        if current:
            window.load_url(current + "/")

    def licences() -> None:
        texts = []
        for name in ("NOTICE", "LICENSE", "THIRD-PARTY-LICENSES.txt"):
            path = resource(name)
            if path.is_file():
                texts.append(
                    f"===== {name} =====\n\n{path.read_text(encoding='utf-8', errors='replace')}"
                )
        body = html.escape("\n\n".join(texts) or "Licence files not found.")
        style = "white-space:pre-wrap;font:12px Consolas,monospace;padding:16px"
        webview.create_window(
            f"{APP} {VERSION}: licences",
            html=f"<pre style='{style}'>{body}</pre>",
            width=760,
            height=700,
        )

    menu = [
        Menu(
            "OpenChart",
            [
                MenuAction("Change server...", change_server),
                MenuAction("Reload", reload_page),
                MenuSeparator(),
                MenuAction(
                    "Open in browser",
                    lambda: webbrowser.open((load_server() or DEFAULT_SERVER) + "/"),
                ),
                MenuAction("About and licences", licences),
            ],
        )
    ]
    storage = app_dir() / "webview"
    storage.mkdir(parents=True, exist_ok=True)
    # Not private: keep the login cookie between launches, like a browser.
    webview.start(gui="edgechromium", private_mode=False, storage_path=str(storage), menu=menu)
    return 0


if __name__ == "__main__":
    sys.exit(main())
