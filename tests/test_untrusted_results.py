"""The server does not trust the worker (CI-2 step b, point 4).

The fake worker answers what a compromised or buggy worker might: outputs that were never
declared, headers that lie, arrays of the wrong length or size, booleans that are not.
Each is an error for that indicator (422), never a crash, a hang or a restart.
"""

import json

import pytest
from fastapi.testclient import TestClient

from openchart.api.app import create_app
from tests.conftest import Harness, run_setup
from tests.test_custom_indicators import catalog, wait_for_worker
from tests.worker_helpers import FakeLauncher


@pytest.fixture(scope="module")
def fc(tmp_path_factory):
    launcher = FakeLauncher()
    harness = Harness(tmp_path_factory.mktemp("untrusted"), indicator_launchers=[launcher])
    harness.seed_candles()
    folder = harness.config.data_dir / "indicators"
    folder.mkdir()
    with TestClient(create_app(harness.server)) as c:
        c.server, c.folder, c.launcher = harness.server, folder, launcher
        assert run_setup(c).status_code == 200
        wait_for_worker(c)
        yield c


def values(c, indicator, **query):
    q = {"instrument": "XAU_USD", "timeframe": "5m", "limit": 20, "params": "{}", **query}
    return c.get(f"/api/indicators/{indicator}", params=q)


def script(c, name, text):
    (c.folder / f"{name}.py").write_text(text, encoding="utf-8")


def test_a_trustworthy_answer_is_accepted(fc):
    script(fc, "fine", "MARKERS\n")
    got = values(fc, "fine").json()
    assert got["hits"] == {"hit": []}
    assert len(got["values"]["out"]) == 20


@pytest.mark.parametrize(
    ("kind", "fragment"),
    [
        ("undeclared", "'zzz', which is not a declared output"),
        ("unlisted", "'zzz', which is not a declared output"),
        ("missing", "no 'hit' output"),
        ("none", "no 'out' output"),
        ("notalist", "does not list its outputs"),
        ("notnames", "does not list its outputs"),
        ("ghost", "does not list its outputs"),
        ("nooutputs", "does not list its outputs"),
        ("duplicate", "does not list its outputs"),
        ("many", "does not list its outputs"),
        ("short", "'out' has 19 values for 20 bars"),
        ("long", "'out' has 40 values for 20 bars"),
        ("huge", "'out' has 3,000,000 values for 20 bars"),
        ("empty", "'out' has 0 values for 20 bars"),
        ("marker2", "'hit' must be true/false"),
        ("markernan", "'hit' must be true/false"),
    ],
)
def test_a_dishonest_answer_is_an_error_for_that_indicator_only(fc, kind, fragment):
    script(fc, "liar", f"MARKERS BAD {kind}\n")
    started = fc.launcher.started
    out = values(fc, "liar")
    assert out.status_code == 422, out.text
    assert out.json()["detail"].startswith("Fake failed: "), out.text
    assert fragment in out.json()["detail"], out.text
    assert fc.get("/api/health").status_code == 200
    script(fc, "fine", "MARKERS\n")
    assert values(fc, "fine").status_code == 200  # the next request is served
    assert fc.launcher.started == started  # by the same worker: nothing was restarted
    assert catalog(fc)[1]["liar"]["status"] != "quarantined"
    (fc.folder / "liar.py").unlink()


def test_bad_answers_never_count_toward_the_quarantine(fc):
    script(fc, "liar2", "MARKERS BAD short\n")
    for k in range(6):
        out = values(fc, "liar2", limit=20 + k)  # different bars: not the cached error
        assert out.status_code == 422
    assert catalog(fc)[1]["liar2"]["status"] == "ok"
    (fc.folder / "liar2.py").unlink()


def test_gaps_only_and_infinities_are_harmless(fc):
    script(fc, "gaps", "MARKERS BAD nanonly\n")
    assert values(fc, "gaps").json()["values"]["out"] == [None] * 20
    script(fc, "infs", "MARKERS BAD inf\n")
    assert values(fc, "infs").json()["values"]["out"] == [None] * 20
    for name in ("gaps", "infs"):
        (fc.folder / f"{name}.py").unlink()


def test_a_definition_the_worker_describes_wrongly_is_not_believed(fc):
    # The fake's own definition is fine; a worker that describes a colour input that is
    # not one is caught by the same rules the worker applies to files.
    from openchart.indicators._custom import indicator_from_json

    script(fc, "fine", "MARKERS\n")
    described = catalog(fc)[1]["fine"]["definition"]
    D = json.loads(json.dumps(described))
    assert D["outputs"][1]["kind"] == "marker"
    bad = {**D, "outputs": [{**D["outputs"][0], "color": "width"}]}
    with pytest.raises(ValueError, match="'width' is not a color parameter"):
        indicator_from_json("fake", bad, file="fake.py")
