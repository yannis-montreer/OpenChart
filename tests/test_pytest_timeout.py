"""A hung test must end the run with a stack dump instead of waiting for the CI job limit
(the Windows 3.13 job of PR #53 sat in `test_alerts_api.py` for 40 minutes)."""

import tomllib
from pathlib import Path

import pytest


def test_the_suite_runs_with_a_timeout(pytestconfig):
    pytest.importorskip("pytest_timeout", reason="pytest-timeout is a dev dependency")
    assert pytestconfig.getini("timeout") == "300"
    assert pytestconfig.getini("timeout_method") == "thread"


def test_pytest_timeout_is_a_dev_dependency():
    pyproject = Path(__file__).resolve().parents[1] / "pyproject.toml"
    dev = tomllib.loads(pyproject.read_text(encoding="utf-8"))["project"]["optional-dependencies"][
        "dev"
    ]
    assert any(d.startswith("pytest-timeout") for d in dev)
