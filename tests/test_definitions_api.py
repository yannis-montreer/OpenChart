"""CI-4's write endpoints for your indicator files (docs/api.md, "Your indicator
definitions"): templates, New, Import, source and Export, Save with its history, Rename,
Duplicate, versions and Restore, Preview; the editing gate; and a hostile-input pass
(path tricks, reserved names, links, sizes, encodings, concurrent writes).

With the fake worker (tests/fake_worker.py): what is tested is what the server stores
and answers; the real worker's running of scripts is tested elsewhere.
"""

import ast
import hashlib
import json
import os
import sys
import threading
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from openchart.api.app import create_app
from openchart.indicators.worker.isolation import Isolation
from openchart.settings import INDICATOR_EDITING
from tests.conftest import Harness, run_setup
from tests.test_custom_indicators import wait_for_worker
from tests.worker_helpers import FakeLauncher
from tests.ws import receive_until

ISOLATED = Isolation("appcontainer", True, "minimum isolation met")
BASE = "/api/custom-indicators"
PLAIN = 'PLAIN\nTITLE = "Gold line"\n'
EXAMPLES = Path(__file__).resolve().parents[1] / "docs" / "examples" / "indicators"


@pytest.fixture
def ec(tmp_path):
    harness = Harness(tmp_path, indicator_launchers=[FakeLauncher()])
    harness.seed_candles()
    folder = harness.config.data_dir / "indicators"
    folder.mkdir()
    with TestClient(create_app(harness.server)) as c:
        c.server, c.folder, c.root = harness.server, folder, tmp_path
        assert run_setup(c).status_code == 200
        wait_for_worker(c)
        c.server.custom.isolation = ISOLATED
        c.portal.call(c.server.settings.set, INDICATOR_EDITING, "on")
        yield c


def version(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()[:12]


def ok(r, status=200):
    assert r.status_code == status, r.text
    return r.json()


def create(c, source=PLAIN, **body):
    return c.post(BASE, json={"source": source, **body})


def history(c, indicator_id):
    folder = c.folder / ".history" / indicator_id
    return sorted(p.name for p in folder.iterdir()) if folder.is_dir() else []


# Templates and New ---------------------------------------------------------------------------


def test_templates_are_blank_and_the_four_examples(ec):
    got = ok(ec.get(f"{BASE}/templates"))
    assert [t["id"] for t in got] == [
        "blank",
        "sma_cross",
        "rsi_bands",
        "bollinger_fill",
        "engulfing",
    ]
    assert all(t["title"] and t["description"] for t in got)


def test_the_templates_are_the_tested_examples():
    """The four examples are tested in test_doc_examples.py; the templates are copies."""
    from openchart.indicators import templates

    folder = Path(templates.__file__).parent
    for name in ("sma_cross", "rsi_bands", "bollinger_fill", "engulfing"):
        assert (folder / f"{name}.py").read_text(encoding="utf-8") == (
            EXAMPLES / f"{name}.py"
        ).read_text(encoding="utf-8"), name


def test_new_from_a_template_with_a_title(ec):
    with ec.websocket_connect("/ws") as ws:
        body = ok(
            ec.post(BASE, json={"id": "my_cross", "template": "sma_cross", "title": "My cross"}),
            201,
        )
        message = receive_until(ws, "indicators_changed")
    assert message == {"type": "indicators_changed", "id": "my_cross", "op": "create"}
    text = (ec.folder / "my_cross.py").read_text(encoding="utf-8")
    assert 'TITLE = "My cross"' in text
    assert body["id"] == "my_cross" and body["kind"] == "custom" and body["file"] == "my_cross.py"
    assert body["version"] == version(text) and body["saved"] is True and body["unchanged"] is False
    assert body["status"] == "ok" and body["error"] is None and body["checks"] == []
    assert history(ec, "my_cross") == []


def test_new_refuses_an_existing_id_and_suggests_a_free_one(ec):
    ok(ec.post(BASE, json={"id": "mine", "template": "blank"}), 201)
    ok(ec.post(BASE, json={"id": "mine_2", "template": "blank"}), 201)
    r = ec.post(BASE, json={"id": "mine", "template": "blank"})
    assert r.status_code == 409 and r.json()["detail"]["suggested"] == "mine_3"
    assert ec.post(BASE, json={"id": "other", "template": "nope"}).status_code == 400


# Import ----------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "filename, expected",
    [
        ("My RSI.py", "my_rsi"),
        ("123.py", "x123"),
        ("_hidden.py", "x_hidden"),
        ("../../evil.py", "evil"),
        ("C:\\Users\\x\\Évolution.py", "evolution"),  # accents dropped
        ("x" * 60 + ".py", "x" * 40),
        ("con.py", "con_"),  # a Windows device name never becomes a file name
        ("NUL.PY", "nul_"),
        (None, "imported"),
    ],
)
def test_import_makes_an_id_from_the_file_name(ec, filename, expected):
    body = {"source": PLAIN} if filename is None else {"source": PLAIN, "filename": filename}
    got = ok(ec.post(BASE, json=body), 201)
    assert got["id"] == expected
    assert (ec.folder / f"{expected}.py").read_text(encoding="utf-8") == PLAIN


def test_import_never_overwrites(ec):
    ok(create(ec, id="mine"), 201)
    r = create(ec, "PLAIN other\n", id="mine")
    assert r.status_code == 409 and r.json()["detail"]["suggested"] == "mine_2"
    assert (ec.folder / "mine.py").read_text(encoding="utf-8") == PLAIN


def test_a_file_that_does_not_load_is_saved_with_its_error(ec):
    got = ok(create(ec, "NODESCRIBE\n", id="broken"), 201)
    assert got["status"] == "error" and got["definition"] is None
    assert got["error"]["type"] == "SyntaxError" and got["error"]["line"] == 1
    assert (ec.folder / "broken.py").is_file()


# Source, Export, Save and the history --------------------------------------------------------


def test_source_and_export(ec):
    ok(create(ec, id="mine"), 201)
    got = ok(ec.get(f"{BASE}/mine/source"))
    assert got["source"] == PLAIN and got["version"] == version(PLAIN)
    assert got["bytes"] == len(PLAIN.encode()) and got["file"] == "mine.py"
    r = ec.get(f"{BASE}/mine/source", params={"download": "true"})
    assert r.status_code == 200 and r.content == PLAIN.encode()
    assert r.headers["content-type"].startswith("text/x-python")
    assert r.headers["content-disposition"] == 'attachment; filename="mine.py"'


def test_save_keeps_the_last_ten_versions(ec):
    ok(create(ec, "PLAIN 0\n", id="mine"), 201)
    with ec.websocket_connect("/ws") as ws:
        ok(ec.put(f"{BASE}/mine/source", json={"source": "PLAIN 1\n"}))
        assert receive_until(ws, "indicators_changed") == {
            "type": "indicators_changed",
            "id": "mine",
            "op": "save",
        }
    for i in range(2, 12):
        ok(ec.put(f"{BASE}/mine/source", json={"source": f"PLAIN {i}\n"}))
    versions = ok(ec.get(f"{BASE}/mine/versions"))
    assert len(versions) == 10 and len(history(ec, "mine")) == 10
    texts = [ok(ec.get(f"{BASE}/mine/versions/{v['version']}"))["source"] for v in versions]
    assert texts == [f"PLAIN {i}\n" for i in range(10, 0, -1)]  # newest first; 0 dropped
    assert versions[0]["digest"] == version("PLAIN 10\n")
    assert versions[0]["bytes"] == len(b"PLAIN 10\n") and versions[0]["saved_at"] > 0


def test_saving_the_same_source_is_unchanged(ec):
    ok(create(ec, id="mine"), 201)
    got = ok(ec.put(f"{BASE}/mine/source", json={"source": PLAIN}))
    assert got["unchanged"] is True and history(ec, "mine") == []


def test_save_refuses_a_stale_base_version(ec):
    ok(create(ec, id="mine"), 201)
    base = version(PLAIN)
    ok(ec.put(f"{BASE}/mine/source", json={"source": "PLAIN a\n", "base_version": base}))
    r = ec.put(f"{BASE}/mine/source", json={"source": "PLAIN b\n", "base_version": base})
    assert r.status_code == 409 and r.json()["detail"]["version"] == version("PLAIN a\n")
    assert (ec.folder / "mine.py").read_text(encoding="utf-8") == "PLAIN a\n"


def test_save_never_creates(ec):
    assert ec.put(f"{BASE}/nope/source", json={"source": PLAIN}).status_code == 404
    assert not (ec.folder / "nope.py").exists()


def test_a_save_is_picked_up_by_the_catalog_and_the_values(ec):
    ok(create(ec, "NODESCRIBE\n", id="mine"), 201)
    got = ok(ec.put(f"{BASE}/mine/source", json={"source": PLAIN}))
    assert got["status"] == "ok"
    custom = {e["id"]: e for e in ec.get("/api/indicators").json()["custom"]}
    assert custom["mine"]["status"] == "ok"
    q = {"instrument": "XAU_USD", "timeframe": "5m", "limit": 10, "kind": "custom"}
    assert ec.get("/api/indicators/mine", params=q).status_code == 200


# Rename, Duplicate, Restore ----------------------------------------------------------------------


@pytest.mark.parametrize(
    "line",
    ['TITLE = "Gold line"', "TITLE='Gold line'", 'TITLE = "Gold line"  # shown in the menu'],
)
def test_rename_edits_the_title_line(ec, line):
    ok(create(ec, f"PLAIN\n{line}\nNAME = 'x'\n", id="mine"), 201)
    title = 'Gold "line" \\ é'
    with ec.websocket_connect("/ws") as ws:
        ok(ec.patch(f"{BASE}/mine", json={"title": title}))
        assert receive_until(ws, "indicators_changed")["op"] == "rename"
    text = (ec.folder / "mine.py").read_text(encoding="utf-8")
    (new,) = [ln for ln in text.splitlines() if ln.startswith("TITLE")]
    assert ast.literal_eval(new.split("=", 1)[1].strip()) == title
    assert text.endswith("NAME = 'x'\n") and len(history(ec, "mine")) == 1


@pytest.mark.parametrize(
    "source",
    [
        "PLAIN\n",  # no TITLE
        'PLAIN\nTITLE = "a"\nTITLE = "b"\n',  # two
        'PLAIN\nTITLE = (\n    "a"\n)\n',  # over several lines
        'PLAIN\nTITLE = "a" + "b"\n',  # not one literal
    ],
)
def test_rename_refuses_a_title_it_cannot_edit(ec, source):
    ok(create(ec, source, id="mine"), 201)
    r = ec.patch(f"{BASE}/mine", json={"title": "New"})
    assert r.status_code == 409 and r.json()["detail"]["message"] == "edit TITLE in the code"
    assert (ec.folder / "mine.py").read_text(encoding="utf-8") == source


@pytest.mark.parametrize("title", ["", "x" * 61, "two\nlines", "tab\there", None, 5])
def test_rename_refuses_a_bad_title(ec, title):
    ok(create(ec, id="mine"), 201)
    assert ec.patch(f"{BASE}/mine", json={"title": title}).status_code in (400, 422)
    assert (ec.folder / "mine.py").read_text(encoding="utf-8") == PLAIN


def test_duplicate(ec):
    ok(create(ec, id="mine"), 201)
    ok(ec.put(f"{BASE}/mine/source", json={"source": PLAIN + "# v2\n"}))
    got = ok(ec.post(f"{BASE}/mine/duplicate", json={}), 201)
    assert got["id"] == "mine_2"
    text = (ec.folder / "mine_2.py").read_text(encoding="utf-8")
    assert 'TITLE = "Gold line (copy)"' in text and text.endswith("# v2\n")
    assert history(ec, "mine_2") == []  # the history is not copied
    assert ok(ec.post(f"{BASE}/mine/duplicate", json={}), 201)["id"] == "mine_3"
    named = ok(ec.post(f"{BASE}/mine/duplicate", json={"id": "gold", "title": "Gold"}), 201)
    assert named["id"] == "gold" and 'TITLE = "Gold"' in (ec.folder / "gold.py").read_text()
    r = ec.post(f"{BASE}/mine/duplicate", json={"id": "gold"})
    assert r.status_code == 409 and r.json()["detail"]["suggested"] == "gold_2"


def test_duplicate_of_a_long_id_stays_an_id(ec):
    long = "x" * 40
    ok(create(ec, id=long), 201)
    assert ok(ec.post(f"{BASE}/{long}/duplicate", json={}), 201)["id"] == "x" * 38 + "_2"


def test_restore_is_a_save(ec):
    ok(create(ec, "PLAIN 0\n", id="mine"), 201)
    ok(ec.put(f"{BASE}/mine/source", json={"source": "PLAIN 1\n"}))
    (old,) = ok(ec.get(f"{BASE}/mine/versions"))
    with ec.websocket_connect("/ws") as ws:
        got = ok(ec.post(f"{BASE}/mine/versions/{old['version']}/restore", json={}))
        assert receive_until(ws, "indicators_changed")["op"] == "restore"
    assert got["version"] == version("PLAIN 0\n")
    assert (ec.folder / "mine.py").read_text(encoding="utf-8") == "PLAIN 0\n"
    texts = [
        ok(ec.get(f"{BASE}/mine/versions/{v['version']}"))["source"]
        for v in ok(ec.get(f"{BASE}/mine/versions"))
    ]
    assert texts == ["PLAIN 1\n", "PLAIN 0\n"]  # the restore can itself be undone


# Preview -------------------------------------------------------------------------------------


def preview(c, source, **extra):
    body = {"source": source, "instrument": "XAU_USD", "timeframe": "5m", "limit": 20, **extra}
    return c.post(f"{BASE}/preview", json=body)


def test_preview_answers_values_and_stores_nothing(ec):
    before = sorted(p.name for p in ec.folder.rglob("*"))
    got = ok(preview(ec, PLAIN))
    assert got["preview"] is True and len(got["t"]) == 20 and len(got["values"]["out"]) == 20
    assert got["definition"]["title"] and got["params"] == {"color": "#2962ff", "width": 1}
    assert sorted(p.name for p in ec.folder.rglob("*")) == before
    assert ec.get("/api/indicators").json()["custom"] == []


@pytest.mark.parametrize(
    "source, kind, line",
    [("NODESCRIBE\n", "SyntaxError", 1), ("RAISE\n", "ZeroDivisionError", 7)],
)
def test_preview_errors_name_their_line(ec, source, kind, line):
    r = preview(ec, source, id="draft")
    assert r.status_code == 422
    detail = r.json()["detail"]
    assert detail["type"] == kind and detail["line"] == line
    assert set(detail) >= {"type", "message", "line", "traceback"}


def test_preview_counts_nothing_toward_a_files_quarantine(ec):
    ok(create(ec, id="mine"), 201)
    for _ in range(3):
        assert preview(ec, "CRASH\n", id="mine").status_code == 422
    entry = {e["id"]: e for e in ec.get("/api/indicators").json()["custom"]}["mine"]
    assert entry["status"] == "ok"
    q = {"instrument": "XAU_USD", "timeframe": "5m", "limit": 10, "kind": "custom"}
    assert ec.get("/api/indicators/mine", params=q).status_code == 200


def test_preview_checks_what_the_worker_answers(ec):
    r = preview(ec, "BAD short\n")
    assert r.status_code == 422 and r.json()["detail"]["type"] == "ResultError"


def test_every_template_loads(ec):
    """Each template is a file that loads (checked as the worker checks it, in-process)."""
    import types

    from openchart.indicators import templates
    from openchart.indicators._registry import from_module

    for template_id in templates.ORDER:
        module = types.ModuleType(f"template_{template_id}")
        exec(compile(templates.source(template_id), f"{template_id}.py", "exec"), module.__dict__)
        assert from_module(template_id, module, builtin=False).id == template_id


def test_preview_refuses_bad_params_and_queries(ec):
    assert preview(ec, PLAIN, params={"width": 9}).status_code == 400
    assert preview(ec, PLAIN, instrument="NOPE").status_code == 404
    assert preview(ec, PLAIN, timeframe="7m").status_code == 400


# The gate --------------------------------------------------------------------------------------


GATED = [
    ("get", f"{BASE}/templates", None),
    ("post", BASE, {"id": "x", "template": "blank"}),
    ("get", f"{BASE}/mine/source", None),
    ("put", f"{BASE}/mine/source", {"source": "PLAIN\n"}),
    ("patch", f"{BASE}/mine", {"title": "T"}),
    ("post", f"{BASE}/mine/duplicate", {}),
    ("get", f"{BASE}/mine/versions", None),
    ("post", f"{BASE}/preview", {"source": "PLAIN\n", "instrument": "XAU_USD"}),
]


@pytest.mark.parametrize("method, path, body", GATED)
@pytest.mark.parametrize("setting", ["auto", "off"])
def test_every_write_endpoint_follows_the_editing_gate(ec, method, path, body, setting):
    ok(create(ec, id="mine"), 201)
    ec.portal.call(ec.server.settings.set, INDICATOR_EDITING, setting)
    kwargs = {"json": body} if body is not None else {}
    r = getattr(ec, method)(path, **kwargs)
    assert r.status_code == 403 and "Editing custom indicators is off" in r.json()["detail"]
    assert (ec.folder / "mine.py").read_text(encoding="utf-8") == PLAIN


def test_on_is_not_enough_below_the_minimum_isolation(ec):
    ec.server.custom.isolation = Isolation("restricted", False, "below the minimum")
    assert ec.post(BASE, json={"id": "x", "template": "blank"}).status_code == 403
    assert not (ec.folder / "x.py").exists()


# Hostile input --------------------------------------------------------------------------------


def outside_snapshot(c):
    """Every file under the test's root that is not in the indicators folder."""
    out = {}
    for p in c.root.rglob("*"):
        if p.is_file() and c.folder not in p.parents and not p.name.startswith("openchart.db"):
            try:
                out[str(p)] = p.read_bytes()
            except PermissionError:  # held open by the server (its lock file): not ours
                out[str(p)] = b"<held>"
    return out


@pytest.mark.parametrize(
    "bad_id",
    [
        "../evil",
        "..",
        ".",
        "evil/x",
        "evil\\x",
        "Mine",
        "mine\n",
        "mine ",
        "_mine",
        "1mine",
        "mine.py",
        "x" * 41,
        "",
        "con",
        "nul",
        "prn",
        "aux",
        "com1",
        "lpt9",
        "conin",
        ".history",
    ],
)
def test_ids_in_a_body_are_checked(ec, bad_id):
    """400 in a body; Windows device names (con, nul, com1 ...) are refused everywhere, so a
    file named after one is never written (on Windows it would be the device)."""
    before = outside_snapshot(ec)
    for body in ({"id": bad_id, "template": "blank"}, {"source": PLAIN, "id": bad_id}):
        r = ec.post(BASE, json=body)
        assert r.status_code == 400, (bad_id, r.status_code, r.text)
    assert sorted(p.name for p in ec.folder.iterdir()) == []
    assert outside_snapshot(ec) == before


@pytest.mark.parametrize("bad", ["..%2F..%2Fevil", "Mine", "mine%0A", "con", "..", "x%5Cy"])
def test_ids_in_a_path_are_404(ec, bad):
    ok(create(ec, id="mine"), 201)
    for method, suffix, body in [
        ("get", "/source", None),
        ("put", "/source", {"source": "PLAIN x\n"}),
        ("patch", "", {"title": "T"}),
        ("post", "/duplicate", {}),
        ("get", "/versions", None),
    ]:
        kwargs = {"json": body} if body is not None else {}
        r = getattr(ec, method)(f"{BASE}/{bad}{suffix}", **kwargs)
        assert r.status_code in (404, 405), (bad, suffix, r.status_code)
    assert (ec.folder / "mine.py").read_text(encoding="utf-8") == PLAIN


@pytest.mark.parametrize("bad", ["../../mine", "20261014T101502Z.py", "x", "20261014T101502Z-0"])
def test_versions_in_a_path_are_checked(ec, bad):
    ok(create(ec, id="mine"), 201)
    ok(ec.put(f"{BASE}/mine/source", json={"source": "PLAIN 1\n"}))
    # 405: the client folds `../..` into another route, which has no such method.
    assert ec.get(f"{BASE}/mine/versions/{bad}").status_code in (404, 405)
    assert ec.post(f"{BASE}/mine/versions/{bad}/restore", json={}).status_code in (404, 405)
    assert (ec.folder / "mine.py").read_text(encoding="utf-8") == "PLAIN 1\n"


def test_the_size_limit(ec):
    limit = 256 * 1024
    just = "#" * (limit - len("PLAIN\n")) + "PLAIN\n"
    assert len(just.encode()) == limit
    ok(create(ec, just, id="big"), 201)
    over = just + "#"
    assert create(ec, over, id="bigger").status_code == 413
    assert ec.put(f"{BASE}/big/source", json={"source": over}).status_code == 413
    two_bytes = "é" * (limit // 2) + "x"  # within 256 K characters, over 256 KB as UTF-8
    assert create(ec, two_bytes, id="wide").status_code == 413
    assert not (ec.folder / "bigger.py").exists() and not (ec.folder / "wide.py").exists()


def test_odd_encodings(ec):
    assert create(ec, "PLAIN\x00\n", id="zero").status_code == 400
    raw = '{"source": "PLAIN \\ud800\\n", "id": "lone"}'  # a lone surrogate
    r = ec.post(BASE, content=raw, headers={"content-type": "application/json"})
    assert r.status_code == 400
    got = ok(create(ec, "\ufeffPLAIN\r\nTITLE = 'é'\r\n", id="bom"), 201)
    assert (ec.folder / "bom.py").read_bytes() == "PLAIN\r\nTITLE = 'é'\r\n".encode()
    assert got["version"] == version("PLAIN\r\nTITLE = 'é'\r\n")
    assert sorted(os.listdir(ec.folder)) == ["bom.py"]  # not exists(): "nul.py" is a device


def test_a_name_that_is_not_a_plain_file_is_never_written(ec):
    (ec.folder / "dir.py").mkdir()
    r = ec.post(BASE, json={"id": "dir", "template": "blank"})
    assert r.status_code == 409
    assert ec.put(f"{BASE}/dir/source", json={"source": PLAIN}).status_code in (404, 409)
    assert (ec.folder / "dir.py").is_dir()


def symlink_or_skip(link: Path, target: Path, *, directory: bool = False):
    try:
        os.symlink(target, link, target_is_directory=directory)
    except (OSError, NotImplementedError):
        pytest.skip("symbolic links need a privilege on this machine")


def test_a_linked_definition_is_never_written_through(ec, tmp_path):
    outside = tmp_path / "outside.py"
    outside.write_text("SECRET\n")
    symlink_or_skip(ec.folder / "linked.py", outside)
    assert ec.put(f"{BASE}/linked/source", json={"source": PLAIN}).status_code == 409
    assert ec.patch(f"{BASE}/linked", json={"title": "T"}).status_code == 409
    assert ec.get(f"{BASE}/linked/source").status_code == 409  # never read through either
    assert ec.post(f"{BASE}/linked/duplicate", json={}).status_code == 409
    assert outside.read_text() == "SECRET\n"
    assert ec.post(BASE, json={"id": "linked", "template": "blank"}).status_code == 409
    assert (ec.folder / "linked.py").is_symlink()


@pytest.mark.skipif(sys.platform != "win32", reason="junctions are Windows'")
def test_a_history_junction_elsewhere_is_never_written_into(ec, tmp_path):
    import _winapi

    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    ok(create(ec, id="mine"), 201)
    _winapi.CreateJunction(str(elsewhere), str(ec.folder / ".history"))
    r = ec.put(f"{BASE}/mine/source", json={"source": "PLAIN x\n"})
    assert r.status_code == 409 and ".history" in r.json()["detail"]
    assert list(elsewhere.iterdir()) == []
    assert (ec.folder / "mine.py").read_text(encoding="utf-8") == PLAIN


def test_a_history_folder_that_is_a_file_is_a_409(ec):
    ok(create(ec, id="mine"), 201)
    (ec.folder / ".history").write_text("not a folder")
    assert ec.put(f"{BASE}/mine/source", json={"source": "PLAIN x\n"}).status_code == 409
    assert (ec.folder / "mine.py").read_text(encoding="utf-8") == PLAIN


def test_a_failed_write_leaves_the_old_file_and_no_temporary_one(ec, monkeypatch):
    ok(create(ec, id="mine"), 201)
    from openchart.indicators import _files

    def broken(src, dst):
        raise PermissionError("the file is open in another program")

    monkeypatch.setattr(_files.os, "replace", broken)
    r = ec.put(f"{BASE}/mine/source", json={"source": "PLAIN new\n"})
    monkeypatch.undo()
    assert r.status_code == 409
    assert (ec.folder / "mine.py").read_text(encoding="utf-8") == PLAIN
    assert sorted(p.name for p in ec.folder.iterdir() if p.is_file()) == ["mine.py"]
    assert history(ec, "mine") == []  # no version kept for a save that did not happen


def test_a_hard_link_to_a_file_elsewhere_is_never_written_through(ec, tmp_path):
    """A hard link is the outside file itself; it needs no privilege on NTFS."""
    outside = tmp_path / "outside.py"
    outside.write_text("SECRET\n")
    try:
        os.link(outside, ec.folder / "hard.py")
    except OSError:
        pytest.skip("hard links are not supported here")
    assert ec.put(f"{BASE}/hard/source", json={"source": PLAIN}).status_code == 409
    assert ec.get(f"{BASE}/hard/source").status_code == 409
    assert outside.read_text() == "SECRET\n"


def test_a_hand_written_file_that_is_not_utf8_is_refused_not_mangled(ec):
    (ec.folder / "latin.py").write_bytes("PLAIN\nTITLE = 'é'\n".encode("latin-1"))
    assert ec.get(f"{BASE}/latin/source").status_code == 409
    assert ec.put(f"{BASE}/latin/source", json={"source": PLAIN}).status_code == 409
    assert (ec.folder / "latin.py").read_bytes() == "PLAIN\nTITLE = 'é'\n".encode("latin-1")


def in_threads(n, fn):
    out = [None] * n
    threads = [threading.Thread(target=lambda i=i: out.__setitem__(i, fn(i))) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)
    return out


def test_concurrent_saves_keep_whole_files_and_whole_versions(ec):
    ok(create(ec, "PLAIN start\n", id="mine"), 201)
    texts = [f"PLAIN {i}\n" + "#" * (20_000 + i) + "\n" for i in range(8)]
    got = in_threads(8, lambda i: ec.put(f"{BASE}/mine/source", json={"source": texts[i]}))
    assert all(r.status_code == 200 for r in got)
    assert (ec.folder / "mine.py").read_text(encoding="utf-8") in texts
    stored = [
        (ec.folder / ".history" / "mine" / n).read_text(encoding="utf-8")
        for n in history(ec, "mine")
    ]
    assert len(stored) == 8 and set(stored) <= {*texts, "PLAIN start\n"}
    assert len(set(stored)) == 8  # every save kept the one before it, none twice
    assert [p.name for p in ec.folder.iterdir() if p.is_file()] == ["mine.py"]


def test_concurrent_saves_from_one_base_let_one_through(ec):
    ok(create(ec, id="mine"), 201)
    base = version(PLAIN)
    got = in_threads(
        4,
        lambda i: ec.put(
            f"{BASE}/mine/source", json={"source": f"PLAIN {i}\n", "base_version": base}
        ),
    )
    assert sorted(r.status_code for r in got) == [200, 409, 409, 409]


def test_concurrent_news_of_one_id_create_it_once(ec):
    got = in_threads(4, lambda i: create(ec, f"PLAIN {i}\n", id="race"))
    assert sorted(r.status_code for r in got) == [201, 409, 409, 409]
    winner = next(r for r in got if r.status_code == 201)
    assert (ec.folder / "race.py").read_text(encoding="utf-8") == ok(ec.get(f"{BASE}/race/source"))[
        "source"
    ]
    assert winner.json()["version"] == version((ec.folder / "race.py").read_text(encoding="utf-8"))


def test_nothing_is_written_outside_the_folder(ec):
    before = outside_snapshot(ec)
    ok(create(ec, id="mine"), 201)
    ok(ec.put(f"{BASE}/mine/source", json={"source": PLAIN + "# 1\n"}))
    ok(ec.patch(f"{BASE}/mine", json={"title": "T"}))
    ok(ec.post(f"{BASE}/mine/duplicate", json={}), 201)
    ok(preview(ec, PLAIN))
    assert outside_snapshot(ec) == before


def test_json_is_valid_in_every_answer(ec):
    """A NaN in a source or a title must not reach a JSON answer as a number."""
    ok(create(ec, "PLAIN\nTITLE = 'nan'\nX = float('nan')\n", id="mine"), 201)
    assert json.loads(ec.get(f"{BASE}/mine/source").text)["source"].endswith("('nan')\n")
