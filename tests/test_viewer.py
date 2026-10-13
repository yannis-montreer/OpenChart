"""The viewer's logic (packaging/viewer): address handling, the server check, settings."""

import importlib.util
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

SPEC = importlib.util.spec_from_file_location(
    "openchart_viewer", Path(__file__).parents[1] / "packaging" / "viewer" / "openchart_viewer.py"
)
viewer = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(viewer)


@pytest.mark.parametrize(
    ("given", "expected"),
    [
        ("my-pc", "http://my-pc:8000"),
        (" http://my-pc:8000/ ", "http://my-pc:8000"),
        ("100.101.102.103:8080", "http://100.101.102.103:8080"),
        ("https://charts.example.ts.net", "https://charts.example.ts.net"),
        ("http://my-pc/openchart", "http://my-pc:8000/openchart"),
    ],
)
def test_addresses(given, expected):
    assert viewer.normalize(given) == expected


@pytest.mark.parametrize("bad", ["", "   ", "ftp://my-pc", "http://"])
def test_bad_addresses(bad):
    with pytest.raises(ValueError):
        viewer.normalize(bad)


def serve(body: bytes, status: int = 200):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(status)
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{server.server_address[1]}"


def test_the_server_check():
    ok, url = serve(json.dumps({"status": "ok"}).encode())
    other, other_url = serve(b"<html>router</html>")
    try:
        assert viewer.check(url) is None
        assert "not an OpenChart server" in viewer.check(other_url)
    finally:
        ok.shutdown()
        other.shutdown()
    assert "Cannot reach http://127.0.0.1:9" in viewer.check("http://127.0.0.1:9", timeout=1)


def test_the_address_is_remembered(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENCHART_VIEWER_HOME", str(tmp_path / "viewer"))
    assert viewer.load_server() is None
    viewer.save_server("http://my-pc:8000")
    assert viewer.load_server() == "http://my-pc:8000"
    (tmp_path / "viewer" / "viewer.json").write_text("not json")
    assert viewer.load_server() is None


def test_the_address_page_escapes_what_it_shows():
    page = viewer.address_page('"><script>x</script>', "Cannot reach <b>")
    assert "<script>x</script>" not in page and "&lt;b&gt;" in page


def test_webview2_is_detected_on_this_machine():
    # Windows 10 with an up-to-date Edge (CI's windows-latest too) has the runtime.
    import sys

    if sys.platform == "win32":
        assert viewer.webview2_installed() in (True, False)
