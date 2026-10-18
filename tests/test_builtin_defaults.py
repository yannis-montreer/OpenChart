"""With default inputs every built-in indicator gives the values it gave when it was added.

`tests/golden/builtin_defaults.json` was written from the first version of these indicators
(before MA types, middle levels and the volume average were added) with:

    python -m tests.test_builtin_defaults

Do not regenerate it to make a failure go away: a change here changes what charts show.
Compared to 1e-12 (relative), not bit for bit, because numpy's summation order may differ
between platforms.
"""

import json
from pathlib import Path

import numpy as np
import pytest

from openchart.indicators._registry import load_indicators
from tests.test_builtin_indicators import gapped, run

GOLDEN = Path(__file__).parent / "golden" / "builtin_defaults.json"
IDS = ["ema", "sma", "rsi", "macd", "bollinger", "atr", "stochastic", "volume"]
FRAMES = {"gapped": lambda: gapped(300, seed=21)}


def current():
    registry = load_indicators()
    out = {}
    for fname, make in FRAMES.items():
        frame = make()
        for oid in IDS:
            ind = registry.get(oid)
            got = run(ind, frame, ind.normalize({}))
            for name, values in got.items():
                out[f"{fname}/{oid}/{name}"] = [None if np.isnan(v) else float(v) for v in values]
    return out


def test_the_golden_file_is_there():
    assert GOLDEN.exists()


@pytest.mark.parametrize("key", sorted(json.loads(GOLDEN.read_text())) if GOLDEN.exists() else [])
def test_default_values_are_unchanged(key):
    want = json.loads(GOLDEN.read_text())[key]
    fname, oid, _ = key.split("/")
    registry = load_indicators()
    ind = registry.get(oid)
    got = run(ind, FRAMES[fname](), ind.normalize({}))[key.split("/")[2]]
    got = np.asarray(got, dtype="float64")
    exp = np.asarray([np.nan if v is None else v for v in want], dtype="float64")
    assert got.shape == exp.shape
    assert (np.isnan(got) == np.isnan(exp)).all()
    ok = ~np.isnan(exp)
    assert np.allclose(got[ok], exp[ok], rtol=1e-12, atol=1e-12)


def test_every_output_with_default_inputs_is_in_the_golden_file():
    keys = set(json.loads(GOLDEN.read_text()))
    # Data outputs only (levels and fills carry no values); new outputs may be added, never lost.
    assert set(current()) >= keys


if __name__ == "__main__":
    GOLDEN.write_text(json.dumps(current(), separators=(",", ":")), encoding="utf-8")
    print("wrote", GOLDEN)
