"""Mutation check of storage steps 2 (docs/decisions-retention.md): each mutant breaks one
rule in a copy of `src/`, and the tests must fail on it. Same runner as `mutate_checks.py`:

    python tools/mutate_retention.py                     # every mutant
    python tools/mutate_retention.py "minute boundary"   # only the ones named

Exit code 0 when every mutant is killed (or marked equivalent), 1 when one survives, 2
when one no longer applies or the tests fail on the unbroken copy.
"""

from __future__ import annotations

import sys

import mutate_checks as runner

TESTS = ["tests/test_retention.py"]
R = "openchart/retention.py"
S = "openchart/store/candles.py"
T = "openchart/store/ticks.py"
V = "openchart/server.py"
M = "openchart/main.py"
F = "openchart/market/feed.py"
A = "openchart/api/routes_account.py"
ST = "openchart/settings.py"


def one(name: str, file: str, old: str, new: str, equivalent: bool = False):
    return (name, file, [(old, new)], equivalent)


MUTANTS = [
    # The limits
    one(
        "cutoff rounds up",
        R,
        "return math.floor(now) - days * DAY",
        "return math.ceil(now) - days * DAY",
    ),
    one(
        "cutoff a day late",
        R,
        "return math.floor(now) - days * DAY",
        "return math.floor(now) - (days - 1) * DAY",
    ),
    one(
        "3 months rounded down", R, "return math.ceil(years * 365.25)", "return int(years * 365.25)"
    ),
    one(
        "default ignores the config",
        R,
        "        return years_to_days(default_years)\n",
        "        return 731\n",
    ),
    one(
        "a tick day goes when it starts before the limit",
        R,
        "if end is None or end > limit or not folder.is_dir():",
        "if end is None or end - DAY > limit or not folder.is_dir():",
    ),
    one(
        "part files do not protect a day",
        R,
        'if any(p.name.startswith("part-") or p.suffix == ".tmp" for p in files):',
        "if False:",
    ),
    # Minutes
    one(
        "minute boundary: the minute holding the limit goes",
        S,
        '"DELETE FROM candles_m1 WHERE series=? AND ts<=?",',
        '"DELETE FROM candles_m1 WHERE series=? AND ts<=?+60",',
    ),
    one(
        "the dry run counts another boundary",
        S,
        "MAX(ts) FROM candles_m1 WHERE series=? AND ts<=?",
        "MAX(ts) FROM candles_m1 WHERE series=? AND ts<?",
    ),
    one(
        "the derived bars are not recomputed",
        S,
        "await self._invalidate_cache(source, instrument, first, max(boundary, cutoff))",
        "pass",
    ),
    one(
        "only up to the limit is recomputed",
        S,
        "await self._invalidate_cache(source, instrument, first, max(boundary, cutoff))",
        "await self._invalidate_cache(source, instrument, first, first)",
    ),
    one(
        "no bump before the delete",
        S,
        "        self._epoch[key] += 1\n        try:\n            cur",
        "        try:\n            cur",
    ),
    one(
        "no bump after the delete",
        S,
        "        finally:\n            self._epoch[key] += 1\n        return deleted",
        "        finally:\n            pass\n        return deleted",
    ),
    # The server's run
    one("no grace", V, "if now < since + retention.GRACE_SECONDS:", "if False:"),
    one(
        "disabled instruments purged too",
        V,
        "rows = await self.store.instruments(self.source, enabled_only=True)\n        plans",
        "rows = await self.store.instruments(self.source)\n        plans",
    ),
    one(
        "ticks deleted when off or forever",
        V,
        "deletes_ticks = days is not None and self.tick_writer is not None",
        "deletes_ticks = self.tick_writer is not None",
    ),
    one(
        "no loop",
        V,
        'create_task(self._retention_loop(), name="retention")',
        'create_task(asyncio.sleep(0), name="retention")',
    ),
    one("once a day: every check", R, "return last is None or utc_day(now) > last", "return True"),
    one(
        "logs not pruned",
        V,
        'retention.prune_service_logs, self.config.data_dir / "logs"',
        "lambda: []",
    ),
    one("six logs kept", R, "SERVICE_LOGS_KEPT = 5", "SERVICE_LOGS_KEPT = 6"),
    one(
        "the live log is pruned too",
        R,
        'folder.glob("service-*.log")',
        'folder.glob("service*.log")',
    ),
    one(
        "the oldest logs kept",
        R,
        "rotated.sort(key=lambda p: (p.stat().st_mtime, p.name), reverse=True)",
        "rotated.sort(key=lambda p: (p.stat().st_mtime, p.name))",
    ),
    one(
        "no log line per tick day",
        T,
        '                log.info(\n                    "retention: ticks of',
        '                log.debug(\n                    "retention: ticks of',
    ),
    # Settings, download, CLI
    one("off still stores", ST, 'store_ticks=retention != "off",', "store_ticks=True,"),
    one(
        "an older page's off ignored",
        A,
        'retention = DEFAULT_TICK_RETENTION if body.store_ticks else "off"',
        "retention = DEFAULT_TICK_RETENTION",
    ),
    one(
        "a longer depth keeps the mark",
        R,
        '    await store.mark_download_complete(source, instrument, "M1", None)\n    return True',
        "    return True",
    ),
    one(
        "the download ignores the depth",
        F,
        "        days = self.m1_depth_days.get(instrument)\n",
        "        days = None\n",
    ),
    one("purge never vacuums", M, '        await conn.execute("VACUUM")\n', ""),
    one(
        "access log on",
        M,
        'return value.strip().lower() in {"1", "true", "yes", "on"}\n\n\ndef uvicorn_server',
        "return True\n\n\ndef uvicorn_server",
    ),
    one(
        "the access log not passed",
        M,
        "log_level=log_level, access_log=access_log_enabled()",
        "log_level=log_level",
    ),
]

if __name__ == "__main__":
    runner.TESTS, runner.MUTANTS = TESTS, MUTANTS
    sys.exit(runner.main(sys.argv[1:]))
