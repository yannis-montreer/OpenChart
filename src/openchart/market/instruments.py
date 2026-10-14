"""Default per-instrument metadata.

The authoritative copy lives in the `instruments` table. These defaults fill in what a
provider does not report (expected trading hours). Adding an instrument should only
need an entry here or a matching provider category; no code changes elsewhere.
"""

from __future__ import annotations

from openchart.market.sessions import NEW_YORK, TradingHours
from openchart.providers.base import InstrumentInfo

# Typical retail forex/metals hours in New York time. Brokers differ by a few minutes;
# these feed the gap report and the feed watchdog, so being close is enough.
# Currency pairs pause for the daily rollover: OANDA's EUR_USD has no candles for 4 to 5
# minutes from 17:00 New York every weekday (and the week opens at 17:05). The holes of
# 2 to 3 minutes that sometimes follow are not modelled.
FX_HOURS = TradingHours(
    NEW_YORK, "sun", "17:00", "fri", "17:00", daily_breaks=(("17:00", "17:05"),)
)
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


def instrument_hours(info: InstrumentInfo) -> TradingHours | None:
    """Hours for a newly enabled instrument: the provider's own, else the defaults."""
    return info.hours or default_hours(info.instrument, info.kind)
