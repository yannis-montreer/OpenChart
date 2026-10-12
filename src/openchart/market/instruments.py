"""Default per-instrument metadata.

The authoritative copy lives in the `instruments` table. These defaults fill in what a
provider does not report (expected trading hours). Adding an instrument should only
need an entry here or a matching provider category; no code changes elsewhere.
"""

from __future__ import annotations

from openchart.market.sessions import NEW_YORK, TradingHours

# Typical retail forex/metals hours in New York time. Brokers differ by a few minutes;
# these feed the gap report and the feed watchdog, so being close is enough.
FX_HOURS = TradingHours(NEW_YORK, "sun", "17:00", "fri", "17:00")
METAL_HOURS = TradingHours(
    NEW_YORK, "sun", "18:00", "fri", "17:00", daily_breaks=(("17:00", "18:00"),)
)

HOURS_BY_KIND: dict[str, TradingHours] = {
    "CURRENCY": FX_HOURS,
    "METAL": METAL_HOURS,
}

# Overrides for specific instruments, keyed by canonical name.
HOURS_BY_INSTRUMENT: dict[str, TradingHours] = {}


def default_hours(instrument: str, kind: str) -> TradingHours | None:
    return HOURS_BY_INSTRUMENT.get(instrument) or HOURS_BY_KIND.get(kind.upper())
