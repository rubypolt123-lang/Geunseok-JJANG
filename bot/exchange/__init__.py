"""Binance USDT-M futures exchange layer (SPEC §6).

Submodules:
- ``bot.exchange.rest``    — ``BinanceRestClient`` (signing, error mapping, retries, weight guard, time sync)
- ``bot.exchange.filters`` — Decimal rounding helpers and ``parse_symbol_filters``
- ``bot.exchange.market``  — ``MarketData`` (public market data), ``klines_to_df``, ``split_closed``

Nothing is imported here on purpose: ``bot.strategy`` / ``bot.risk`` import only ``bot.exchange.filters``
(§0.2 import layering), so importing the package must not pull in ``requests`` or the REST client.
"""

from __future__ import annotations
