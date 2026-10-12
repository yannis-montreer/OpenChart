"""Builds the configured provider."""

from __future__ import annotations

from openchart.config import Config
from openchart.providers.base import Provider


def create_provider(config: Config) -> Provider:
    from openchart.providers.oanda import OandaProvider

    return OandaProvider(config.oanda)
