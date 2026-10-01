"""Brokers: where orders are executed (SPEC §9.2-§9.5).

- ``Broker``          — abstract interface driven by the trader (``bot/broker/base.py``)
- ``PaperBroker``     — local simulation on real public market data; never signs (``bot/broker/paper.py``)
- ``ExchangeBroker``  — Binance USDT-M futures, testnet (Demo Trading) and live (``bot/broker/exchange_broker.py``)
"""

from __future__ import annotations

from bot.broker.base import Broker
from bot.broker.exchange_broker import ExchangeBroker
from bot.broker.paper import PaperBroker

__all__ = ["Broker", "ExchangeBroker", "PaperBroker"]
