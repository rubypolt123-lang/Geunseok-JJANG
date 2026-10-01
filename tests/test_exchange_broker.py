"""ExchangeBroker (SPEC §9.4, §9.5) against an in-memory Binance stand-in served through ``responses``.

Fake keys only ("k"*64 / "s"*64); every HTTP request is intercepted by ``responses`` (no network). The stand-in
keeps a tiny exchange state (one-way position, regular orders, algo orders, fills, income) so that request
sequences behave realistically; individual endpoints can be scripted to inject exchange errors.
"""

from __future__ import annotations

import dataclasses
import json
import logging
import re
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any
from urllib.parse import parse_qsl, urlsplit

import pytest
import responses

from bot.broker.exchange_broker import ExchangeBroker
from bot.config import MAINNET_REST_URL, TESTNET_REST_URL, AppConfig
from bot.errors import ConfigError, EmergencyError, ProtectionFailedError, TransientError
from bot.exchange.market import MarketData
from bot.exchange.rest import BinanceRestClient
from bot.models import (
    ActiveTrade,
    Direction,
    ExitReason,
    Mode,
    OpenOutcome,
    OrderPurpose,
    OrderStatus,
    Side,
    TradePlan,
    client_id_prefix,
    make_client_id,
    next_client_id,
)

SYMBOL = "BTCUSDT"
KEY = "k" * 64
SECRET = "s" * 64
BOT = "mab1"
H = 3_600_000
START_S = 1_790_769_600.0
BAR = 1_790_766_000_000  # last closed bar (decision bar)
ENTRY_BAR = BAR + H  # the entry fills in the next bar (== FakeClock start)
PREFIX = client_id_prefix(BOT, SYMBOL)
SIGNATURE_KEYS = frozenset({"recvWindow", "timestamp", "signature"})
TAKER = Decimal("0.0005")


def cid(kind: str, bar: int = ENTRY_BAR, seq: int = 0) -> str:
    return make_client_id(BOT, SYMBOL, kind, bar, seq)


EN = cid("EN", BAR)
SL1 = cid("SL", ENTRY_BAR, 1)
TP1 = cid("TP", ENTRY_BAR, 1)
SL2 = cid("SL", ENTRY_BAR, 2)
TP2 = cid("TP", ENTRY_BAR, 2)


# ---------------------------------------------------------------------------------------------
# Binance stand-in
# ---------------------------------------------------------------------------------------------


@dataclass
class Reply:
    status: int = 200
    body: Any = None
    text: str | None = None
    headers: dict[str, str] = field(default_factory=dict)


def err(code: int, msg: str = "error", *, status: int = 400, headers: dict[str, str] | None = None) -> Reply:
    return Reply(status, {"code": code, "msg": msg}, headers=headers or {})


UNKNOWN_503 = Reply(503, text="Unknown error, please check your request or try again later.")
TRANSIENT_500 = err(-1001, "Internal error; unable to process your request. Please try again.", status=500)
NO_SUCH = err(-2013, "Order does not exist.")
DEFAULT = object()  # in a script: let the stand-in's normal handler answer


@dataclass(frozen=True)
class Call:
    method: str
    path: str
    items: tuple[tuple[str, str], ...]  # query params in order, without recvWindow/timestamp/signature
    t: int  # fake-clock ms when the request arrived

    @property
    def params(self) -> dict[str, str]:
        return dict(self.items)

    @property
    def keys(self) -> list[str]:
        return [k for k, _ in self.items]


def fmt(d: Decimal) -> str:
    return format(d, "f")


class FakeBinance:
    """Minimal one-way-mode USDT-M futures exchange for a single symbol."""

    def __init__(self, rsps: responses.RequestsMock, clock: Any, exchange_info: dict[str, Any], *, base: str) -> None:
        self.clock = clock
        self.exchange_info = exchange_info
        self.calls: list[Call] = []
        self.scripts: dict[tuple[str, str], list[Any]] = {}
        self.dual = False
        self.multi = False
        self.margin_type = "ISOLATED"
        self.leverage = 3
        self.wallet = Decimal("10000")
        self.position_amt = Decimal("0")
        self.entry_price = Decimal("0")
        self.mark_price = Decimal("0")
        self.position_update_time = 0
        self.fill_price = Decimal("84000.0")
        self.fill_prices: list[Decimal] = []  # per MARKET order (overrides fill_price while non-empty)
        self.fill_limits: list[Decimal] = []  # per MARKET order: max executed qty (partial fill -> EXPIRED)
        self.orders: list[dict[str, Any]] = []
        self.algos: list[dict[str, Any]] = []
        self.trades: list[dict[str, Any]] = []
        self.income: list[dict[str, Any]] = []
        self.wrap_open_algo = False
        self._next_order_id = 1001
        self._next_algo_id = 5001
        self._next_trade_id = 9001
        self.handlers: dict[tuple[str, str], Callable[[dict[str, str]], Any]] = {
            ("GET", "/fapi/v1/time"): lambda p: {"serverTime": self.now},
            ("GET", "/fapi/v1/exchangeInfo"): lambda p: self.exchange_info,
            ("GET", "/fapi/v1/accountConfig"): self._account_config,
            ("POST", "/fapi/v1/positionSide/dual"): self._set_dual,
            ("POST", "/fapi/v1/multiAssetsMargin"): self._set_multi,
            ("GET", "/fapi/v1/symbolConfig"): self._symbol_config,
            ("POST", "/fapi/v1/marginType"): self._set_margin_type,
            ("POST", "/fapi/v1/leverage"): self._set_leverage,
            ("GET", "/fapi/v3/positionRisk"): self._position_risk,
            ("GET", "/fapi/v3/account"): self._account,
            ("POST", "/fapi/v1/order"): self._new_order,
            ("GET", "/fapi/v1/order"): self._get_order,
            ("DELETE", "/fapi/v1/order"): self._cancel_order,
            ("GET", "/fapi/v1/openOrders"): self._open_orders,
            ("POST", "/fapi/v1/algoOrder"): self._new_algo,
            ("GET", "/fapi/v1/algoOrder"): self._get_algo,
            ("DELETE", "/fapi/v1/algoOrder"): self._cancel_algo,
            ("GET", "/fapi/v1/openAlgoOrders"): self._open_algos,
            ("GET", "/fapi/v1/allAlgoOrders"): self._all_algos,
            ("GET", "/fapi/v1/userTrades"): self._user_trades,
            ("GET", "/fapi/v1/income"): self._income,
        }
        pattern = re.compile(re.escape(base) + r"/.*")
        for method in (responses.GET, responses.POST, responses.DELETE, responses.PUT):
            rsps.add_callback(method, pattern, callback=self._dispatch)

    # -- plumbing ---------------------------------------------------------------------------------

    @property
    def now(self) -> int:
        return int(round(self.clock() * 1000))

    def script(self, method: str, path: str, *replies: Any) -> None:
        """Queue replies (Reply / JSON value / callable(params) / DEFAULT) consumed before the normal handler."""
        self.scripts.setdefault((method, path), []).extend(replies)

    def run_default(self, method: str, path: str, params: dict[str, str]) -> Any:
        return self.handlers[(method, path)](params)

    def _dispatch(self, request: Any) -> tuple[int, dict[str, str], str]:
        parts = urlsplit(request.url)
        items = tuple((k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if k not in SIGNATURE_KEYS)
        call = Call(str(request.method), parts.path, items, self.now)
        self.calls.append(call)
        key = (call.method, call.path)
        queue = self.scripts.get(key)
        reply: Any = queue.pop(0) if queue else DEFAULT
        if callable(reply) and not isinstance(reply, Reply):
            reply = reply(call.params)
        if reply is DEFAULT:
            handler = self.handlers.get(key)
            if handler is None:
                raise AssertionError(f"unexpected request {call.method} {call.path} {call.params}")
            reply = handler(call.params)
        if isinstance(reply, Reply):
            if reply.text is not None:
                return reply.status, {"Content-Type": "text/html", **reply.headers}, reply.text
            return reply.status, {"Content-Type": "application/json", **reply.headers}, json.dumps(reply.body)
        return 200, {"Content-Type": "application/json"}, json.dumps(reply)

    def calls_to(self, method: str, path: str) -> list[Call]:
        return [c for c in self.calls if c.method == method and c.path == path]

    def idx(self, method: str, path: str, **params: str) -> int:
        for i, c in enumerate(self.calls):
            if c.method == method and c.path == path and all(c.params.get(k) == v for k, v in params.items()):
                return i
        raise AssertionError(f"no call {method} {path} {params}")

    def trading_calls(self) -> list[tuple[str, str]]:
        return [(c.method, c.path) for c in self.calls if c.path != "/fapi/v1/time"]

    # -- state helpers --------------------------------------------------------------------------

    def max_notional(self, leverage: int) -> int:
        return 240_000_000 // int(leverage)

    def set_position(self, amt: str, entry: str, *, update_time: int | None = None) -> None:
        self.position_amt = Decimal(amt)
        self.entry_price = Decimal(entry)
        self.mark_price = Decimal(entry)
        self.position_update_time = self.now if update_time is None else update_time

    def add_algo(
        self,
        client_algo_id: str,
        order_type: str,
        side: str,
        trigger: str,
        *,
        status: str = "NEW",
        close_position: bool = True,
        quantity: str = "0.000",
        create_time: int | None = None,
    ) -> dict[str, Any]:
        algo = {
            "algoId": self._next_algo_id,
            "clientAlgoId": client_algo_id,
            "algoType": "CONDITIONAL",
            "orderType": order_type,
            "symbol": SYMBOL,
            "side": side,
            "positionSide": "BOTH",
            "timeInForce": "GTC",
            "quantity": quantity,
            "algoStatus": status,
            "triggerPrice": trigger,
            "price": "0.00",
            "workingType": "MARK_PRICE",
            "priceProtect": False,
            "closePosition": close_position,
            "reduceOnly": not close_position,
            "createTime": self.now if create_time is None else create_time,
            "updateTime": self.now,
            "actualOrderId": "",
        }
        self._next_algo_id += 1
        self.algos.append(algo)
        return algo

    def add_open_order(self, client_order_id: str, *, side: str = "BUY", price: str = "80000", qty: str = "0.010") -> None:
        order = self._make_order(client_order_id, side, Decimal(qty), reduce_only=False)
        order.update({"type": "LIMIT", "origType": "LIMIT", "price": price, "status": "NEW"})
        self.orders.append(order)

    def add_trade(self, order_id: int, side: str, price: str, qty: str, *, time: int, realized: str = "0") -> None:
        p, q = Decimal(price), Decimal(qty)
        self.trades.append(self._trade_row(order_id, side, p, q, Decimal(realized), q * p * TAKER, time))

    def add_income(self, income_type: str, amount: str, *, time: int) -> None:
        self.income.append(
            {
                "symbol": SYMBOL,
                "incomeType": income_type,
                "income": amount,
                "asset": "USDT",
                "info": "",
                "time": time,
                "tranId": len(self.income) + 1,
                "tradeId": "",
            }
        )

    def find_order(self, order_id: int) -> dict[str, Any]:
        return next(o for o in self.orders if o["orderId"] == order_id)

    def algo(self, client_algo_id: str) -> dict[str, Any]:
        return [a for a in self.algos if a["clientAlgoId"] == client_algo_id][-1]

    def trigger_algo(self, client_algo_id: str, price: str) -> dict[str, Any]:
        """The stop/take-profit fires: a market order closes the position, the algo order finishes."""
        algo = self.algo(client_algo_id)
        qty = abs(self.position_amt) if algo["closePosition"] else Decimal(algo["quantity"])
        order = self._make_order(f"x-algo-{algo['algoId']}", algo["side"], qty, reduce_only=True)
        self._fill(order, qty, Decimal(price))
        order["status"] = "FILLED"
        self.orders.append(order)
        algo.update({"algoStatus": "FINISHED", "actualOrderId": order["orderId"], "updateTime": self.now})
        return order

    # -- position / fills -------------------------------------------------------------------------

    def _apply_position(self, signed: Decimal, price: Decimal) -> Decimal:
        pos = self.position_amt
        realized = Decimal("0")
        if pos == 0 or (pos > 0) == (signed > 0):
            new = pos + signed
            self.entry_price = (abs(pos) * self.entry_price + abs(signed) * price) / abs(new)
        else:
            closing = min(abs(signed), abs(pos))
            realized = closing * (price - self.entry_price) * (1 if pos > 0 else -1)
            new = pos + signed
            if new == 0:
                self.entry_price = Decimal("0")
            elif (new > 0) != (pos > 0):
                self.entry_price = price
        self.position_amt = new
        self.mark_price = price
        self.position_update_time = self.now
        return realized

    def _trade_row(
        self, order_id: int, side: str, price: Decimal, qty: Decimal, realized: Decimal, commission: Decimal, time: int
    ) -> dict[str, Any]:
        row = {
            "symbol": SYMBOL,
            "id": self._next_trade_id,
            "orderId": order_id,
            "side": side,
            "price": fmt(price),
            "qty": fmt(qty),
            "quoteQty": fmt(price * qty),
            "realizedPnl": fmt(realized),
            "commission": fmt(commission),
            "commissionAsset": "USDT",
            "marginAsset": "USDT",
            "positionSide": "BOTH",
            "buyer": side == "BUY",
            "maker": False,
            "time": time,
        }
        self._next_trade_id += 1
        return row

    def _fill(self, order: dict[str, Any], qty: Decimal, price: Decimal) -> None:
        signed = qty if order["side"] == "BUY" else -qty
        realized = self._apply_position(signed, price)
        commission = qty * price * TAKER
        self.trades.append(self._trade_row(order["orderId"], order["side"], price, qty, realized, commission, self.now))
        self.wallet += realized - commission
        executed = Decimal(order["executedQty"]) + qty
        cum = Decimal(order["cumQuote"]) + qty * price
        order.update(
            {"executedQty": fmt(executed), "cumQuote": fmt(cum), "avgPrice": fmt(cum / executed), "updateTime": self.now}
        )

    def _make_order(self, client_order_id: str, side: str, qty: Decimal, *, reduce_only: bool) -> dict[str, Any]:
        order = {
            "orderId": self._next_order_id,
            "clientOrderId": client_order_id,
            "symbol": SYMBOL,
            "status": "NEW",
            "side": side,
            "type": "MARKET",
            "origType": "MARKET",
            "positionSide": "BOTH",
            "reduceOnly": reduce_only,
            "closePosition": False,
            "origQty": fmt(qty),
            "executedQty": "0",
            "cumQuote": "0",
            "avgPrice": "0",
            "price": "0",
            "timeInForce": "GTC",
            "time": self.now,
            "updateTime": self.now,
        }
        self._next_order_id += 1
        return order

    # -- handlers -------------------------------------------------------------------------------

    def _account_config(self, p: dict[str, str]) -> Any:
        return {"feeTier": 0, "canTrade": True, "dualSidePosition": self.dual, "multiAssetsMargin": self.multi}

    def _set_dual(self, p: dict[str, str]) -> Any:
        want = p["dualSidePosition"] == "true"
        if want == self.dual:
            return err(-4059, "No need to change position side.")
        self.dual = want
        return {"code": 200, "msg": "success"}

    def _set_multi(self, p: dict[str, str]) -> Any:
        want = p["multiAssetsMargin"] == "true"
        if want == self.multi:
            return err(-4171, "Multi-Assets Mode is already set.")
        self.multi = want
        return {"code": 200, "msg": "success"}

    def _symbol_config(self, p: dict[str, str]) -> Any:
        return [
            {
                "symbol": SYMBOL,
                "marginType": self.margin_type,
                "isAutoAddMargin": False,
                "leverage": self.leverage,
                "maxNotionalValue": str(self.max_notional(self.leverage)),
            }
        ]

    def _set_margin_type(self, p: dict[str, str]) -> Any:
        if p["marginType"] == self.margin_type:
            return err(-4046, "No need to change margin type.")
        if self.position_amt != 0:
            return err(-4048, "Margin type cannot be changed if there exists position.")
        self.margin_type = p["marginType"]
        return {"code": 200, "msg": "success"}

    def _set_leverage(self, p: dict[str, str]) -> Any:
        lev = int(p["leverage"])
        if not 1 <= lev <= 125:
            return err(-4028, "Leverage is not valid")
        if self.position_amt != 0 and self.margin_type == "ISOLATED" and lev < self.leverage:
            return err(-4161, "Leverage reduction is not supported in Isolated Margin Mode with open positions.")
        self.leverage = lev
        return {"leverage": lev, "maxNotionalValue": str(self.max_notional(lev)), "symbol": SYMBOL}

    def _position_risk(self, p: dict[str, str]) -> Any:
        amt = self.position_amt
        if amt == 0:
            row = {"symbol": SYMBOL, "positionSide": "BOTH", "positionAmt": "0.000", "entryPrice": "0.0",
                   "markPrice": fmt(self.mark_price), "unRealizedProfit": "0.00000000", "liquidationPrice": "0",
                   "isolatedMargin": "0", "updateTime": 0}
            return [row]
        upnl = amt * (self.mark_price - self.entry_price)
        margin = abs(amt) * self.entry_price / self.leverage
        liq = self.entry_price * (1 - Decimal(1) / self.leverage) if amt > 0 else self.entry_price * (1 + Decimal(1) / self.leverage)
        return [
            {
                "symbol": SYMBOL,
                "positionSide": "BOTH",
                "positionAmt": fmt(amt),
                "entryPrice": fmt(self.entry_price),
                "breakEvenPrice": fmt(self.entry_price),
                "markPrice": fmt(self.mark_price),
                "unRealizedProfit": fmt(upnl),
                "liquidationPrice": fmt(liq),
                "isolatedMargin": fmt(margin),
                "notional": fmt(amt * self.mark_price),
                "marginAsset": "USDT",
                "updateTime": self.position_update_time,
            }
        ]

    def _account(self, p: dict[str, str]) -> Any:
        upnl = self.position_amt * (self.mark_price - self.entry_price) if self.position_amt else Decimal("0")
        margin = abs(self.position_amt) * self.entry_price / self.leverage if self.position_amt else Decimal("0")
        return {
            "totalWalletBalance": fmt(self.wallet),
            "totalUnrealizedProfit": fmt(upnl),
            "totalMarginBalance": fmt(self.wallet + upnl),
            "availableBalance": fmt(self.wallet - margin),
            "assets": [
                {"asset": "BNB", "walletBalance": "1.0", "unrealizedProfit": "0", "marginBalance": "1.0",
                 "availableBalance": "1.0"},
                {
                    "asset": "USDT",
                    "walletBalance": fmt(self.wallet),
                    "unrealizedProfit": fmt(upnl),
                    "marginBalance": fmt(self.wallet + upnl),
                    "availableBalance": fmt(self.wallet - margin),
                },
            ],
            "positions": [],
        }

    def _new_order(self, p: dict[str, str]) -> Any:
        if p.get("type") != "MARKET":
            return err(-4120, "Order type not supported for this endpoint. Please use the Algo Order API endpoints instead.")
        client_order_id = p["newClientOrderId"]
        if any(o["clientOrderId"] == client_order_id for o in self.orders):
            return err(-4116, "ClientOrderId is duplicated.")
        qty = Decimal(p["quantity"])
        reduce_only = p.get("reduceOnly") == "true"
        signed = qty if p["side"] == "BUY" else -qty
        target = qty
        if reduce_only:
            if self.position_amt == 0 or (self.position_amt > 0) == (signed > 0):
                return err(-2022, "ReduceOnly Order is rejected.")
            target = min(qty, abs(self.position_amt))
        executed = target
        if self.fill_limits:
            executed = min(executed, self.fill_limits.pop(0))
        price = self.fill_prices.pop(0) if self.fill_prices else self.fill_price
        order = self._make_order(client_order_id, p["side"], qty, reduce_only=reduce_only)
        if executed > 0:
            self._fill(order, executed, price)
        order["status"] = "FILLED" if executed == target else "EXPIRED"
        self.orders.append(order)
        return dict(order)

    def _get_order(self, p: dict[str, str]) -> Any:
        for o in reversed(self.orders):
            if ("orderId" in p and str(o["orderId"]) == p["orderId"]) or (
                "origClientOrderId" in p and o["clientOrderId"] == p["origClientOrderId"]
            ):
                return dict(o)
        return NO_SUCH

    def _cancel_order(self, p: dict[str, str]) -> Any:
        for o in self.orders:
            if o["clientOrderId"] == p.get("origClientOrderId") and o["status"] == "NEW":
                o["status"] = "CANCELED"
                return dict(o)
        return err(-2011, "Unknown order sent.")

    def _open_orders(self, p: dict[str, str]) -> Any:
        return [dict(o) for o in self.orders if o["status"] in ("NEW", "PARTIALLY_FILLED") and o["symbol"] == p["symbol"]]

    def _new_algo(self, p: dict[str, str]) -> Any:
        assert p.get("algoType") == "CONDITIONAL"
        if "stopPrice" in p:
            return err(-1104, "Not all sent parameters were read.")
        if p.get("closePosition") == "true" and ("quantity" in p or "reduceOnly" in p):
            return err(-4137, "Quantity must be zero with closePosition equals true.")
        client_algo_id = p["clientAlgoId"]
        if any(a["clientAlgoId"] == client_algo_id and a["algoStatus"] in ("NEW", "TRIGGERING") for a in self.algos):
            return err(-4116, "ClientOrderId is duplicated.")
        algo = self.add_algo(
            client_algo_id,
            p["type"],
            p["side"],
            p["triggerPrice"],
            close_position=p.get("closePosition") == "true",
            quantity=p.get("quantity", "0.000"),
        )
        algo["workingType"] = p.get("workingType", "CONTRACT_PRICE")
        algo["priceProtect"] = p.get("priceProtect") == "true"
        return {k: v for k, v in algo.items() if k != "actualOrderId"}

    def _get_algo(self, p: dict[str, str]) -> Any:
        for a in reversed(self.algos):
            if a["clientAlgoId"] == p.get("clientAlgoId") or str(a["algoId"]) == p.get("algoId"):
                return dict(a)
        return NO_SUCH

    def _cancel_algo(self, p: dict[str, str]) -> Any:
        for a in self.algos:
            if a["clientAlgoId"] == p.get("clientAlgoId") and a["algoStatus"] in ("NEW", "TRIGGERING"):
                a["algoStatus"] = "CANCELED"
                return {"algoId": a["algoId"], "clientAlgoId": a["clientAlgoId"], "code": "200", "msg": "success"}
        return err(-2011, "Unknown order sent.")

    def _open_algos(self, p: dict[str, str]) -> Any:
        rows = [
            {k: v for k, v in a.items() if k != "actualOrderId"}
            for a in self.algos
            if a["algoStatus"] in ("NEW", "TRIGGERING") and a["symbol"] == p["symbol"]
        ]
        return {"orders": rows} if self.wrap_open_algo else rows

    def _all_algos(self, p: dict[str, str]) -> Any:
        return [dict(a) for a in self.algos if a["symbol"] == p["symbol"]]

    def _user_trades(self, p: dict[str, str]) -> Any:
        rows = [t for t in self.trades if t["symbol"] == p["symbol"]]
        if "orderId" in p:
            rows = [t for t in rows if str(t["orderId"]) == p["orderId"]]
        if "startTime" in p:
            rows = [t for t in rows if t["time"] >= int(p["startTime"])]
        if "endTime" in p:
            rows = [t for t in rows if t["time"] <= int(p["endTime"])]
        return rows[: int(p.get("limit", 500))]

    def _income(self, p: dict[str, str]) -> Any:
        rows = sorted(
            (
                r
                for r in self.income
                if r["symbol"] == p.get("symbol", SYMBOL)
                and r["incomeType"] == p.get("incomeType", r["incomeType"])
                and int(p.get("startTime", 0)) <= r["time"] <= int(p.get("endTime", 2**62))
            ),
            key=lambda r: r["time"],
        )
        return rows[: int(p.get("limit", 100))]


# ---------------------------------------------------------------------------------------------
# Fixtures and helpers
# ---------------------------------------------------------------------------------------------


@pytest.fixture
def clock(fixed_clock: Callable[..., Any]) -> Any:
    return fixed_clock(START_S)


@pytest.fixture
def rsps() -> Iterator[responses.RequestsMock]:
    with responses.RequestsMock(assert_all_requests_are_fired=False) as mock:
        yield mock


@pytest.fixture
def fake(rsps: responses.RequestsMock, clock: Any, exchange_info_btc: dict[str, Any]) -> FakeBinance:
    return FakeBinance(rsps, clock, exchange_info_btc, base=TESTNET_REST_URL)


@pytest.fixture
def live_fake(rsps: responses.RequestsMock, clock: Any, exchange_info_btc: dict[str, Any]) -> FakeBinance:
    return FakeBinance(rsps, clock, exchange_info_btc, base=MAINNET_REST_URL)


@pytest.fixture
def make_broker(app_config: AppConfig, clock: Any, fake: FakeBinance) -> Iterator[Callable[..., ExchangeBroker]]:
    clients: list[BinanceRestClient] = []

    def make(
        *,
        mode: Mode = Mode.TESTNET,
        protective_mode: str = "close_position",
        prepare: bool = False,
        leverage: int = 3,
    ) -> ExchangeBroker:
        base = TESTNET_REST_URL if mode is Mode.TESTNET else MAINNET_REST_URL
        client = BinanceRestClient(base, KEY, SECRET, clock=clock, sleep=clock.sleep)
        clients.append(client)
        execution = dataclasses.replace(app_config.execution, protective_mode=protective_mode)
        broker = ExchangeBroker(
            mode=mode, client=client, market=MarketData(client), execution=execution, clock=clock, sleep=clock.sleep
        )
        if prepare:
            broker.prepare_symbol(SYMBOL, leverage)
            fake.calls.clear()
            clock.sleeps.clear()
        return broker

    yield make
    for c in clients:
        c.close()


def make_plan(
    direction: Direction = Direction.LONG,
    *,
    qty: str = "0.010",
    stop: str | None = None,
    tp: str | None = "default",
    ref: float = 84_000.0,
) -> TradePlan:
    long = direction is Direction.LONG
    stop = stop or ("82000.0" if long else "86000.0")
    if tp == "default":
        tp = "88000.0" if long else "80000.0"
    q = Decimal(qty)
    return TradePlan(
        symbol=SYMBOL,
        direction=direction,
        ref_price=ref,
        qty=q,
        stop_price=Decimal(stop),
        take_profit_price=None if tp is None else Decimal(tp),
        notional=float(q) * ref,
        risk_amount=float(q) * abs(ref - float(stop)) + 1.0,
        leverage=3,
        liquidation_price=56_300.0 if long else 111_700.0,
    )


def open_with(
    broker: ExchangeBroker, plan: TradePlan, *, en: str = EN, sl: str = SL1, tp: str | None = TP1
) -> OpenOutcome:
    return broker.open_position(
        plan,
        entry_client_id=en,
        sl_client_id=sl,
        tp_client_id=tp if plan.take_profit_price is not None else None,
        ref_price=plan.ref_price,
        bar_time=ENTRY_BAR,
    )


def active_from(plan: TradePlan, outcome: OpenOutcome) -> ActiveTrade:
    """Exactly what the trader builds (§10.1 ``_active_from_entry``): protect_seq 1, entry_order_id set."""
    return ActiveTrade(
        trade_id=f"testnet-{SYMBOL}-{ENTRY_BAR}-{'L' if plan.direction is Direction.LONG else 'S'}",
        symbol=SYMBOL,
        direction=plan.direction,
        qty=outcome.qty,
        entry_price=outcome.avg_price,
        entry_time=outcome.entry_time,
        entry_bar_open_time=ENTRY_BAR,
        stop_price=float(plan.stop_price),
        take_profit_price=None if plan.take_profit_price is None else float(plan.take_profit_price),
        liquidation_price=plan.liquidation_price,
        leverage=plan.leverage,
        risk_amount=plan.risk_amount * outcome.qty / float(plan.qty),
        entry_fee=outcome.entry_fee,
        entry_client_id=EN,
        protect_seq=1,
        entry_order_id=outcome.entry_order.exchange_id if outcome.entry_order else None,
    )


def make_active(
    *, qty: float = 0.010, entry_price: float = 84_000.0, entry_time: int = ENTRY_BAR + 500,
    take_profit: float | None = 88_000.0, entry_order_id: str | None = "1001",
) -> ActiveTrade:
    return ActiveTrade(
        trade_id=f"testnet-{SYMBOL}-{ENTRY_BAR}-L",
        symbol=SYMBOL,
        direction=Direction.LONG,
        qty=qty,
        entry_price=entry_price,
        entry_time=entry_time,
        entry_bar_open_time=ENTRY_BAR,
        stop_price=82_000.0,
        take_profit_price=take_profit,
        liquidation_price=56_300.0,
        leverage=3,
        risk_amount=20.84,
        entry_fee=0.42,
        entry_client_id=EN,
        protect_seq=1,
        entry_order_id=entry_order_id,
    )


def make_adopted(*, qty: float, entry_price: float, updated_at: int) -> ActiveTrade:
    """The trader's adopted-position shape (§10.1 _handle_sync step 2): protect_seq 0, no entry order id."""
    return ActiveTrade(
        trade_id=f"testnet-{SYMBOL}-adopted-{updated_at}",
        symbol=SYMBOL,
        direction=Direction.LONG,
        qty=qty,
        entry_price=entry_price,
        entry_time=updated_at,
        entry_bar_open_time=updated_at - updated_at % H,
        stop_price=82_000.0,
        take_profit_price=88_000.0,
        liquidation_price=None,
        leverage=3,
        risk_amount=qty * 2_000.0,
        entry_fee=0.0,
        entry_client_id="adopted",
        protect_seq=0,
        entry_order_id=None,
    )


# ---------------------------------------------------------------------------------------------
# Construction and prepare_symbol
# ---------------------------------------------------------------------------------------------


def test_refuses_paper_mode_and_host_mismatch(
    app_config: AppConfig, clock: Any, rsps: responses.RequestsMock
) -> None:
    execution = app_config.execution
    demo = BinanceRestClient(TESTNET_REST_URL, KEY, SECRET, clock=clock, sleep=clock.sleep)
    main = BinanceRestClient(MAINNET_REST_URL, KEY, SECRET, clock=clock, sleep=clock.sleep)
    keyless = BinanceRestClient(TESTNET_REST_URL, clock=clock, sleep=clock.sleep)
    try:
        with pytest.raises(ConfigError):
            ExchangeBroker(mode=Mode.PAPER, client=demo, market=MarketData(demo), execution=execution)
        with pytest.raises(ConfigError, match="host/mode mismatch"):
            ExchangeBroker(mode=Mode.TESTNET, client=main, market=MarketData(main), execution=execution)
        with pytest.raises(ConfigError, match="host/mode mismatch"):
            ExchangeBroker(mode=Mode.LIVE, client=demo, market=MarketData(demo), execution=execution)
        with pytest.raises(ConfigError, match="host/mode mismatch"):
            ExchangeBroker(mode=Mode.TESTNET, client=demo, market=MarketData(main), execution=execution)
        with pytest.raises(ConfigError, match="credentials"):
            ExchangeBroker(mode=Mode.TESTNET, client=keyless, market=MarketData(keyless), execution=execution)
        ok = ExchangeBroker(mode=Mode.TESTNET, client=demo, market=MarketData(demo), execution=execution)
        assert ok.mode is Mode.TESTNET
        assert ok.max_notional(SYMBOL) is None
        live = ExchangeBroker(mode=Mode.LIVE, client=main, market=MarketData(main), execution=execution)
        assert live.mode is Mode.LIVE
        assert len(rsps.calls) == 0  # construction never talks to the exchange
    finally:
        for c in (demo, main, keyless):
            c.close()


def test_prepare_symbol_sequence_and_no_change_codes(
    fake: FakeBinance, make_broker: Callable[..., ExchangeBroker]
) -> None:
    fake.dual = True
    fake.multi = True
    fake.margin_type = "CROSSED"
    fake.leverage = 20

    def dual_no_change(p: dict[str, str]) -> Reply:
        fake.dual = False
        return err(-4059, "No need to change position side.")

    def margin_no_change(p: dict[str, str]) -> Reply:
        fake.margin_type = "ISOLATED"
        return err(-4046, "No need to change margin type.")

    fake.script("POST", "/fapi/v1/positionSide/dual", dual_no_change)
    fake.script("POST", "/fapi/v1/marginType", margin_no_change)
    broker = make_broker()
    filters = broker.prepare_symbol(SYMBOL, 3)

    assert filters.symbol == SYMBOL and filters.tick_size == Decimal("0.10")
    assert [(c.method, c.path) for c in fake.calls] == [
        ("GET", "/fapi/v1/time"),
        ("GET", "/fapi/v1/exchangeInfo"),
        ("GET", "/fapi/v1/accountConfig"),
        ("POST", "/fapi/v1/positionSide/dual"),
        ("POST", "/fapi/v1/multiAssetsMargin"),
        ("GET", "/fapi/v1/symbolConfig"),
        ("GET", "/fapi/v3/positionRisk"),
        ("POST", "/fapi/v1/marginType"),
        ("POST", "/fapi/v1/leverage"),
        ("GET", "/fapi/v1/symbolConfig"),
    ]
    assert fake.calls_to("POST", "/fapi/v1/positionSide/dual")[0].params == {"dualSidePosition": "false"}
    assert fake.calls_to("POST", "/fapi/v1/multiAssetsMargin")[0].params == {"multiAssetsMargin": "false"}
    assert fake.calls_to("POST", "/fapi/v1/marginType")[0].items == (("symbol", SYMBOL), ("marginType", "ISOLATED"))
    assert fake.calls_to("POST", "/fapi/v1/leverage")[0].items == (("symbol", SYMBOL), ("leverage", "3"))
    assert fake.leverage == 3 and not fake.dual and not fake.multi
    assert broker.max_notional(SYMBOL) == 80_000_000.0

    # the verification step catches a configuration that did not stick
    fake.script("GET", "/fapi/v1/symbolConfig", DEFAULT, lambda p: [{**fake._symbol_config(p)[0], "leverage": 5}])
    with pytest.raises(ConfigError, match="not applied"):
        broker.prepare_symbol(SYMBOL, 3)


@pytest.mark.parametrize("code", [-4067, -4068, -4531])
def test_prepare_symbol_hedge_mode_blocked_raises(
    fake: FakeBinance, make_broker: Callable[..., ExchangeBroker], code: int
) -> None:
    fake.dual = True
    fake.script("POST", "/fapi/v1/positionSide/dual", err(code, "Position side cannot be changed."))
    broker = make_broker()
    with pytest.raises(ConfigError, match="Hedge mode"):
        broker.prepare_symbol(SYMBOL, 3)
    assert not fake.calls_to("POST", "/fapi/v1/leverage")
    assert not fake.calls_to("POST", "/fapi/v1/marginType")


@pytest.mark.parametrize(
    ("dual", "multi", "hint"), [(True, False, "단방향"), (False, True, "멀티에셋"), (True, True, "단방향")]
)
def test_prepare_symbol_live_never_changes_account_modes(
    live_fake: FakeBinance, make_broker: Callable[..., ExchangeBroker], dual: bool, multi: bool, hint: str
) -> None:
    live_fake.dual = dual
    live_fake.multi = multi
    broker = make_broker(mode=Mode.LIVE)
    with pytest.raises(ConfigError, match=hint):
        broker.prepare_symbol(SYMBOL, 3)
    assert [c for c in live_fake.calls if c.method != "GET"] == []  # nothing was changed on mainnet
    assert live_fake.dual is dual and live_fake.multi is multi


def test_prepare_symbol_keeps_leverage_with_open_position(
    fake: FakeBinance, make_broker: Callable[..., ExchangeBroker], caplog: pytest.LogCaptureFixture
) -> None:
    # A: a position exists at a higher leverage -> never reduced, no /leverage POST at all
    fake.leverage = 5
    fake.set_position("0.010", "84000")
    broker = make_broker()
    with caplog.at_level(logging.WARNING, logger="bot.broker.exchange_broker"):
        broker.prepare_symbol(SYMBOL, 3)
    assert not fake.calls_to("POST", "/fapi/v1/leverage")
    assert "keeping exchange leverage 5 for the open position; configured 3 applies when flat" in caplog.text
    assert broker.max_notional(SYMBOL) == 48_000_000.0  # from symbolConfig of the kept leverage
    acct = broker.sync(SYMBOL, make_active(), []).account
    assert acct.position is not None and acct.position.leverage == 5

    # B: the position appears between the reads -> -4161 on /leverage is tolerated the same way
    caplog.clear()
    fake.calls.clear()
    fake.set_position("0", "0")
    fake.script("POST", "/fapi/v1/leverage", err(-4161, "Leverage reduction is not supported in Isolated Margin Mode with open positions."))
    other = make_broker()
    with caplog.at_level(logging.WARNING, logger="bot.broker.exchange_broker"):
        other.prepare_symbol(SYMBOL, 3)
    assert len(fake.calls_to("POST", "/fapi/v1/leverage")) == 1
    assert "keeping exchange leverage 5" in caplog.text
    assert other.max_notional(SYMBOL) == 48_000_000.0

    # C: an invalid leverage is a configuration error
    fake.script("POST", "/fapi/v1/leverage", err(-4028, "Leverage 3 is not valid"))
    with pytest.raises(ConfigError, match="invalid leverage"):
        make_broker().prepare_symbol(SYMBOL, 3)


def test_leverage_applied_when_flat_before_entry(fake: FakeBinance, make_broker: Callable[..., ExchangeBroker]) -> None:
    fake.leverage = 5
    fake.set_position("0.010", "84000")
    broker = make_broker()
    broker.prepare_symbol(SYMBOL, 3)  # keeps 5 for the open position
    fake.set_position("0", "0")  # the position was closed (e.g. by its stop)
    fake.calls.clear()

    out = open_with(broker, make_plan())
    assert out.filled
    lev = fake.idx("POST", "/fapi/v1/leverage", leverage="3")
    assert lev < fake.idx("GET", "/fapi/v1/order") < fake.idx("POST", "/fapi/v1/order")
    assert fake.leverage == 3
    assert broker.max_notional(SYMBOL) == 80_000_000.0
    assert broker.sync(SYMBOL, active_from(make_plan(), out), []).account.position.leverage == 3  # type: ignore[union-attr]

    # a failed catch-up never sends the entry
    broker.close_position(SYMBOL, None, reason=ExitReason.SIGNAL, client_id=cid("EX", BAR), ref_price=None, bar_time=ENTRY_BAR)
    broker._leverage[SYMBOL] = 5  # as if kept for an earlier position
    fake.calls.clear()
    fake.script("POST", "/fapi/v1/leverage", err(-4028, "Leverage is not valid"))
    out2 = open_with(broker, make_plan(), en=cid("EN", BAR + H))
    assert out2.filled is False
    assert out2.message.startswith("leverage update failed")
    assert not fake.calls_to("POST", "/fapi/v1/order")


def test_max_notional_cached_from_leverage_response(fake: FakeBinance, make_broker: Callable[..., ExchangeBroker]) -> None:
    broker = make_broker()
    assert broker.max_notional(SYMBOL) is None
    fake.script("POST", "/fapi/v1/leverage", lambda p: {"leverage": 3, "maxNotionalValue": "1234567", "symbol": SYMBOL})
    broker.prepare_symbol(SYMBOL, 3)
    assert broker.max_notional(SYMBOL) == 1_234_567.0


# ---------------------------------------------------------------------------------------------
# open_position
# ---------------------------------------------------------------------------------------------


def test_open_sends_market_then_algo_stop(fake: FakeBinance, make_broker: Callable[..., ExchangeBroker]) -> None:
    broker = make_broker(prepare=True)
    out = open_with(broker, make_plan(tp=None))

    assert fake.trading_calls() == [
        ("GET", "/fapi/v1/order"),  # pre-flight idempotency lookup
        ("POST", "/fapi/v1/order"),
        ("GET", "/fapi/v3/positionRisk"),  # position truth check
        ("GET", "/fapi/v1/userTrades"),  # entry fee
        ("POST", "/fapi/v1/algoOrder"),
    ]
    assert fake.calls_to("GET", "/fapi/v1/order")[0].params == {"symbol": SYMBOL, "origClientOrderId": EN}
    assert fake.calls_to("POST", "/fapi/v1/order")[0].items == (
        ("symbol", SYMBOL),
        ("side", "BUY"),
        ("type", "MARKET"),
        ("quantity", "0.01"),
        ("newClientOrderId", EN),
        ("newOrderRespType", "RESULT"),
    )
    assert fake.calls_to("POST", "/fapi/v1/algoOrder")[0].items == (
        ("algoType", "CONDITIONAL"),
        ("symbol", SYMBOL),
        ("side", "SELL"),
        ("type", "STOP_MARKET"),
        ("triggerPrice", "82000"),
        ("workingType", "MARK_PRICE"),
        ("priceProtect", "false"),
        ("closePosition", "true"),
        ("clientAlgoId", SL1),
    )
    assert not fake.calls_to("GET", "/fapi/v1/openAlgoOrders")  # the POST answer (algoStatus NEW) is enough

    assert out.filled is True
    assert out.qty == pytest.approx(0.01)
    assert out.avg_price == pytest.approx(84_000.0)
    assert out.entry_fee == pytest.approx(0.42)  # USDT commission from userTrades
    assert out.entry_time == fake.find_order(1001)["updateTime"]
    assert out.entry_order is not None
    assert out.entry_order.exchange_id == "1001"
    assert out.entry_order.client_id == EN
    assert out.entry_order.status is OrderStatus.FILLED
    assert out.entry_order.purpose is OrderPurpose.ENTRY
    assert len(out.protective) == 1
    sl = out.protective[0]
    assert (sl.kind, sl.client_id, sl.exchange_id, sl.status) == (OrderPurpose.STOP_LOSS, SL1, "5001", "NEW")
    assert sl.side is Side.SELL and sl.trigger_price == 82_000.0 and sl.close_position and sl.quantity is None


def test_stop_orders_never_sent_to_order_endpoint(fake: FakeBinance, make_broker: Callable[..., ExchangeBroker]) -> None:
    broker = make_broker(prepare=True)
    plan = make_plan()
    out = open_with(broker, plan)
    active = active_from(plan, out)
    # the stop disappears; the protection check replaces it; then the position is closed
    fake.algo(SL1)["algoStatus"] = "CANCELED"
    res = broker.sync(SYMBOL, active, [])
    assert "SL_MISSING" in res.issues
    broker.ensure_protection(active, res.account, sl_client_id=SL2, tp_client_id=TP2)
    broker.close_position(SYMBOL, active, reason=ExitReason.SIGNAL, client_id=cid("EX", BAR + H), ref_price=None, bar_time=ENTRY_BAR + H)

    for c in fake.calls_to("POST", "/fapi/v1/order"):
        assert c.params["type"] == "MARKET"
    for c in fake.calls:
        assert "stopPrice" not in c.params
        if c.params.get("type") in {"STOP_MARKET", "TAKE_PROFIT_MARKET", "STOP", "TAKE_PROFIT"}:
            assert (c.method, c.path) == ("POST", "/fapi/v1/algoOrder")
        assert c.path not in {"/fapi/v1/allOpenOrders", "/fapi/v1/algoOpenOrders"}
    assert len(fake.calls_to("POST", "/fapi/v1/algoOrder")) == 3  # SL + TP, then the replacement SL


def test_take_profit_placed_when_planned(fake: FakeBinance, make_broker: Callable[..., ExchangeBroker]) -> None:
    broker = make_broker(prepare=True)
    out = open_with(broker, make_plan())
    algo = fake.calls_to("POST", "/fapi/v1/algoOrder")
    assert [c.params["clientAlgoId"] for c in algo] == [SL1, TP1]
    assert algo[1].items == (
        ("algoType", "CONDITIONAL"),
        ("symbol", SYMBOL),
        ("side", "SELL"),
        ("type", "TAKE_PROFIT_MARKET"),
        ("triggerPrice", "88000"),
        ("workingType", "MARK_PRICE"),
        ("priceProtect", "false"),
        ("closePosition", "true"),
        ("clientAlgoId", TP1),
    )
    assert [p.kind for p in out.protective] == [OrderPurpose.STOP_LOSS, OrderPurpose.TAKE_PROFIT]

    # short: both protective orders BUY, SL above / TP below
    broker.close_position(SYMBOL, None, reason=ExitReason.SIGNAL, client_id=cid("EX", BAR), ref_price=None, bar_time=ENTRY_BAR)
    fake.calls.clear()
    short = open_with(
        broker, make_plan(Direction.SHORT), en=cid("EN", BAR + H), sl=cid("SL", ENTRY_BAR + H, 1), tp=cid("TP", ENTRY_BAR + H, 1)
    )
    algo = fake.calls_to("POST", "/fapi/v1/algoOrder")
    assert [(c.params["side"], c.params["type"], c.params["triggerPrice"]) for c in algo] == [
        ("BUY", "STOP_MARKET", "86000"),
        ("BUY", "TAKE_PROFIT_MARKET", "80000"),
    ]
    assert fake.calls_to("POST", "/fapi/v1/order")[0].params["side"] == "SELL"
    assert short.filled and len(short.protective) == 2


def test_take_profit_failure_keeps_position(fake: FakeBinance, make_broker: Callable[..., ExchangeBroker]) -> None:
    broker = make_broker(prepare=True)
    fake.script("POST", "/fapi/v1/algoOrder", DEFAULT, err(-2021, "Order would immediately trigger."))
    out = open_with(broker, make_plan())
    assert out.filled
    assert [p.kind for p in out.protective] == [OrderPurpose.STOP_LOSS]
    assert len(fake.calls_to("POST", "/fapi/v1/order")) == 1  # no flatten: the SL exists
    assert fake.position_amt == Decimal("0.010")


def test_reduce_only_protective_mode_params(fake: FakeBinance, make_broker: Callable[..., ExchangeBroker]) -> None:
    broker = make_broker(protective_mode="reduce_only", prepare=True)
    fake.fill_limits = [Decimal("0.006")]  # the entry expires after a partial fill -> position 0.006
    out = open_with(broker, make_plan())
    assert out.filled and out.qty == pytest.approx(0.006)
    algo = fake.calls_to("POST", "/fapi/v1/algoOrder")
    assert algo[0].items == (
        ("algoType", "CONDITIONAL"),
        ("symbol", SYMBOL),
        ("side", "SELL"),
        ("type", "STOP_MARKET"),
        ("triggerPrice", "82000"),
        ("workingType", "MARK_PRICE"),
        ("priceProtect", "false"),
        ("quantity", "0.006"),  # abs(positionAmt)
        ("reduceOnly", "true"),
        ("clientAlgoId", SL1),
    )
    assert algo[1].params["quantity"] == "0.006" and "closePosition" not in algo[1].keys
    sl = out.protective[0]
    assert sl.close_position is False and sl.quantity == pytest.approx(0.006)


def test_sl_failure_flattens_and_raises(fake: FakeBinance, make_broker: Callable[..., ExchangeBroker]) -> None:
    broker = make_broker(prepare=True)
    fake.script("POST", "/fapi/v1/algoOrder", err(-1111, "Precision is over the maximum defined for this asset."))
    with pytest.raises(ProtectionFailedError) as info:
        open_with(broker, make_plan())
    e = info.value
    assert e.flattened is True
    assert e.entry is not None and e.entry.filled and e.entry.qty == pytest.approx(0.01)
    assert e.entry.entry_order is not None and e.entry.entry_order.exchange_id == "1001"
    assert e.closure is not None
    assert e.closure.reason is ExitReason.PROTECTION_FAILED
    assert e.closure.qty == pytest.approx(0.01)
    assert len(fake.calls_to("POST", "/fapi/v1/algoOrder")) == 1  # a definitive rejection is not retried
    close = fake.calls_to("POST", "/fapi/v1/order")[1]
    assert close.params == {
        "symbol": SYMBOL,
        "side": "SELL",
        "type": "MARKET",
        "quantity": "0.01",
        "reduceOnly": "true",
        "newClientOrderId": make_client_id(BOT, SYMBOL, "FL", ENTRY_BAR, 0),
        "newOrderRespType": "RESULT",
    }
    assert fake.position_amt == 0


def test_sl_immediate_trigger_flattens(fake: FakeBinance, make_broker: Callable[..., ExchangeBroker]) -> None:
    broker = make_broker(prepare=True)
    fake.script("POST", "/fapi/v1/algoOrder", err(-2021, "Order would immediately trigger."))
    with pytest.raises(ProtectionFailedError) as info:
        open_with(broker, make_plan())
    assert info.value.closure is not None and info.value.closure.reason is ExitReason.PROTECTION_FAILED
    assert len(fake.calls_to("POST", "/fapi/v1/algoOrder")) == 1
    assert not fake.calls_to("GET", "/fapi/v1/algoOrder")  # no lookup, no retry
    assert fake.position_amt == 0


def test_sl_failure_and_flatten_failure_raises_emergency(
    fake: FakeBinance, make_broker: Callable[..., ExchangeBroker]
) -> None:
    broker = make_broker(prepare=True)
    fake.script("POST", "/fapi/v1/algoOrder", err(-2021, "Order would immediately trigger."))
    reject = err(-4131, "The counterparty's best price does not meet the PERCENT_PRICE filter limit.")
    fake.script("POST", "/fapi/v1/order", DEFAULT, reject, reject, reject)
    with pytest.raises(EmergencyError) as info:
        open_with(broker, make_plan())
    assert info.value.entry is not None and info.value.entry.filled
    fl0 = make_client_id(BOT, SYMBOL, "FL", ENTRY_BAR, 0)
    sent = [c.params["newClientOrderId"] for c in fake.calls_to("POST", "/fapi/v1/order")[1:]]
    assert sent == [fl0, next_client_id(fl0), next_client_id(next_client_id(fl0))]  # every attempt a fresh id
    assert fake.position_amt == Decimal("0.010")


def test_sl_post_unknown_status_found_by_client_algo_id_no_flatten(
    fake: FakeBinance, make_broker: Callable[..., ExchangeBroker]
) -> None:
    broker = make_broker(prepare=True)

    def placed_but_unknown(p: dict[str, str]) -> Reply:
        fake.run_default("POST", "/fapi/v1/algoOrder", p)
        return UNKNOWN_503

    fake.script("POST", "/fapi/v1/algoOrder", placed_but_unknown)
    out = open_with(broker, make_plan(tp=None))
    assert out.filled
    assert out.protective[0].client_id == SL1 and out.protective[0].status == "NEW"
    assert len(fake.calls_to("POST", "/fapi/v1/algoOrder")) == 1  # never blindly re-sent
    assert [c.params for c in fake.calls_to("GET", "/fapi/v1/algoOrder")] == [{"clientAlgoId": SL1}]
    assert len(fake.calls_to("POST", "/fapi/v1/order")) == 1  # no flatten
    assert fake.position_amt == Decimal("0.010")


def test_sl_post_transient_then_absent_retries_once(
    fake: FakeBinance, make_broker: Callable[..., ExchangeBroker], clock: Any
) -> None:
    broker = make_broker(prepare=True)
    fake.script("POST", "/fapi/v1/algoOrder", TRANSIENT_500)  # -1001: not executed, but nobody can know
    out = open_with(broker, make_plan(tp=None))
    posts = fake.calls_to("POST", "/fapi/v1/algoOrder")
    assert len(posts) == 2
    assert posts[0].items == posts[1].items  # the retry reuses the same clientAlgoId
    assert len(fake.calls_to("GET", "/fapi/v1/algoOrder")) == 3  # 3 lookups before the single retry
    assert clock.sleeps == [0.5, 0.5]
    assert out.filled and out.protective[0].status == "NEW"
    assert len(fake.calls_to("POST", "/fapi/v1/order")) == 1


def test_sl_duplicate_id_existing_order_counts_as_placed(
    fake: FakeBinance, make_broker: Callable[..., ExchangeBroker]
) -> None:
    broker = make_broker(prepare=True)
    earlier = fake.add_algo(SL1, "STOP_MARKET", "SELL", "82000.0")  # placed by an earlier attempt
    out = open_with(broker, make_plan(tp=None))
    assert len(fake.calls_to("POST", "/fapi/v1/algoOrder")) == 1  # answered -4116
    assert out.filled
    assert out.protective[0].exchange_id == str(earlier["algoId"])
    assert len([a for a in fake.algos if a["clientAlgoId"] == SL1]) == 1
    assert len(fake.calls_to("POST", "/fapi/v1/order")) == 1


def test_sl_rate_limit_sleeps_before_retry(
    fake: FakeBinance, make_broker: Callable[..., ExchangeBroker], clock: Any
) -> None:
    broker = make_broker(prepare=True)
    fake.script("POST", "/fapi/v1/algoOrder", err(-1003, "Too many requests.", status=429, headers={"Retry-After": "30"}))
    out = open_with(broker, make_plan(tp=None))
    assert clock.sleeps[0] == 10.0  # min(retry_after, 10)
    post, retry = fake.calls_to("POST", "/fapi/v1/algoOrder")
    first_lookup = fake.calls_to("GET", "/fapi/v1/algoOrder")[0]
    assert first_lookup.t - post.t == 10_000  # waited BEFORE resolving
    assert retry.params["clientAlgoId"] == SL1
    assert out.filled and out.protective[0].client_id == SL1


def test_sl_lookup_triggered_is_success(fake: FakeBinance, make_broker: Callable[..., ExchangeBroker]) -> None:
    broker = make_broker(prepare=True)

    def placed_and_fired(p: dict[str, str]) -> Reply:
        fake.run_default("POST", "/fapi/v1/algoOrder", p)
        fake.algos[-1]["algoStatus"] = "TRIGGERED"
        return UNKNOWN_503

    fake.script("POST", "/fapi/v1/algoOrder", placed_and_fired)
    out = open_with(broker, make_plan(tp=None))
    assert out.filled
    assert out.protective[0].status == "TRIGGERED"
    assert len(fake.calls_to("POST", "/fapi/v1/algoOrder")) == 1
    assert len(fake.calls_to("POST", "/fapi/v1/order")) == 1  # no flatten


def test_algo_limit_cleans_own_stale_orders_and_retries(
    fake: FakeBinance, make_broker: Callable[..., ExchangeBroker]
) -> None:
    broker = make_broker(prepare=True)
    stale_short_sl = fake.add_algo(cid("SL", BAR - 5 * H, 1), "STOP_MARKET", "BUY", "90000.0", create_time=1)
    old_sl = fake.add_algo(cid("SL", BAR - 9 * H, 1), "STOP_MARKET", "SELL", "70000.0", create_time=2)
    newer_sl = fake.add_algo(cid("SL", BAR - 2 * H, 1), "STOP_MARKET", "SELL", "71000.0", create_time=3)
    foreign = fake.add_algo("web_abc", "STOP_MARKET", "SELL", "60000.0", create_time=4)
    fake.script("POST", "/fapi/v1/algoOrder", err(-4045, "Reach max stop order limit."))
    out = open_with(broker, make_plan(tp=None))
    assert out.filled and out.protective[0].client_id == SL1
    cancelled = {c.params["clientAlgoId"] for c in fake.calls_to("DELETE", "/fapi/v1/algoOrder")}
    assert cancelled == {stale_short_sl["clientAlgoId"], old_sl["clientAlgoId"]}
    assert newer_sl["algoStatus"] == "NEW" and foreign["algoStatus"] == "NEW"
    assert len(fake.calls_to("POST", "/fapi/v1/algoOrder")) == 2


def test_entry_unknown_status_queries_by_client_id(fake: FakeBinance, make_broker: Callable[..., ExchangeBroker]) -> None:
    broker = make_broker(prepare=True)

    def filled_but_unknown(p: dict[str, str]) -> Reply:
        fake.run_default("POST", "/fapi/v1/order", p)
        return UNKNOWN_503

    fake.script("POST", "/fapi/v1/order", filled_but_unknown)
    out = open_with(broker, make_plan(tp=None))
    assert len(fake.calls_to("POST", "/fapi/v1/order")) == 1  # never re-sent blindly
    lookups = [c for c in fake.calls_to("GET", "/fapi/v1/order") if c.params.get("origClientOrderId") == EN]
    assert len(lookups) == 2  # pre-flight + the resolving lookup
    assert fake.idx("POST", "/fapi/v1/order") < fake.calls.index(lookups[1])
    assert out.filled
    assert out.entry_order is not None and out.entry_order.client_id == EN and out.entry_order.exchange_id == "1001"
    assert out.protective[0].client_id == SL1


def test_entry_unknown_then_position_exists_is_protected(
    fake: FakeBinance, make_broker: Callable[..., ExchangeBroker], clock: Any
) -> None:
    broker = make_broker(prepare=True)

    def filled_but_unknown(p: dict[str, str]) -> Reply:
        fake.run_default("POST", "/fapi/v1/order", p)
        return UNKNOWN_503

    fake.script("POST", "/fapi/v1/order", filled_but_unknown)
    fake.script("GET", "/fapi/v1/order", NO_SUCH, NO_SUCH, NO_SUCH, NO_SUCH)  # pre-flight + 3 lookups find nothing
    out = open_with(broker, make_plan(tp=None))
    assert clock.sleeps == [1.0, 1.0]  # lookups 1 s apart
    assert out.filled is True
    assert out.message == "recovered"
    assert out.entry_order is None
    assert out.qty == pytest.approx(0.01)  # abs(positionAmt)
    assert out.avg_price == pytest.approx(84_000.0)  # position entryPrice
    assert out.entry_fee == pytest.approx(0.01 * 84_000.0 * 0.0005)  # estimated
    assert out.protective[0].kind is OrderPurpose.STOP_LOSS
    assert fake.algo(SL1)["algoStatus"] == "NEW"


def test_entry_not_final_is_polled_to_terminal(
    fake: FakeBinance, make_broker: Callable[..., ExchangeBroker], clock: Any
) -> None:
    broker = make_broker(prepare=True)

    def accepted_not_final(p: dict[str, str]) -> Any:
        resp = fake.run_default("POST", "/fapi/v1/order", p)
        return {**resp, "status": "NEW", "executedQty": "0", "avgPrice": "0", "cumQuote": "0"}

    fake.script("POST", "/fapi/v1/order", accepted_not_final)
    fake.script(
        "GET",
        "/fapi/v1/order",
        DEFAULT,  # pre-flight (not found)
        lambda p: {**fake.find_order(1001), "status": "PARTIALLY_FILLED", "executedQty": "0.004"},
        DEFAULT,  # FILLED
    )
    out = open_with(broker, make_plan(tp=None))
    polls = [c for c in fake.calls_to("GET", "/fapi/v1/order") if "orderId" in c.params]
    assert [c.params for c in polls] == [{"symbol": SYMBOL, "orderId": "1001"}] * 2
    assert clock.sleeps == [1.0, 1.0]
    assert out.filled and out.qty == pytest.approx(0.01)
    assert out.entry_order is not None and out.entry_order.status is OrderStatus.FILLED
    assert fake.idx("GET", "/fapi/v1/order", orderId="1001") < fake.idx("GET", "/fapi/v3/positionRisk")


def test_filled_false_only_after_flat_confirmed(
    fake: FakeBinance, make_broker: Callable[..., ExchangeBroker], clock: Any
) -> None:
    broker = make_broker(prepare=True)

    # A: a definitive rejection -> filled=False, but only after positionRisk confirmed flat
    fake.script("POST", "/fapi/v1/order", err(-2019, "Margin is insufficient."))
    out = open_with(broker, make_plan(), en=cid("EN", BAR - 3 * H))
    assert out.filled is False
    assert out.message == "-2019 Margin is insufficient."
    assert fake.idx("POST", "/fapi/v1/order") < fake.idx("GET", "/fapi/v3/positionRisk")
    assert not fake.calls_to("POST", "/fapi/v1/algoOrder")

    # B: outcome unknown, never found, flat -> filled=False after the truth check
    fake.calls.clear()
    clock.sleeps.clear()
    fake.script("POST", "/fapi/v1/order", UNKNOWN_503)
    out = open_with(broker, make_plan(), en=cid("EN", BAR - 2 * H))
    assert out.filled is False and out.message.startswith("entry outcome unknown")
    assert clock.sleeps == [1.0, 1.0]
    assert fake.calls[-1].path == "/fapi/v3/positionRisk"

    # C: the position read fails -> no filled=False without confirmation (the error propagates)
    fake.calls.clear()
    fake.script("POST", "/fapi/v1/order", err(-2019, "Margin is insufficient."))
    fake.script("GET", "/fapi/v3/positionRisk", *[Reply(503, text="Service Unavailable")] * 4)
    with pytest.raises(TransientError):
        open_with(broker, make_plan(), en=cid("EN", BAR - H))

    # D: "rejected" but a position exists anyway -> it is filled (and protected)
    fake.calls.clear()

    def filled_but_rejected(p: dict[str, str]) -> Reply:
        fake.run_default("POST", "/fapi/v1/order", p)
        return err(-2019, "Margin is insufficient.")

    fake.script("POST", "/fapi/v1/order", filled_but_rejected)
    out = open_with(broker, make_plan(tp=None))
    assert out.filled is True
    assert out.protective[0].client_id == SL1


def test_opposite_position_is_left_untouched(fake: FakeBinance, make_broker: Callable[..., ExchangeBroker]) -> None:
    broker = make_broker(prepare=True)
    fake.set_position("-0.020", "85000")  # appeared from elsewhere
    fake.fill_limits = [Decimal("0")]  # our long entry does not fill
    out = open_with(broker, make_plan())
    assert out.filled is False and out.message == "unexpected opposite position"
    assert not fake.calls_to("POST", "/fapi/v1/algoOrder")
    assert fake.position_amt == Decimal("-0.020")


def test_preflight_existing_fill_not_resent(fake: FakeBinance, make_broker: Callable[..., ExchangeBroker]) -> None:
    broker = make_broker(prepare=True)
    # an earlier attempt with the same id expired after a partial fill (marketTakeBound)
    fake.fill_limits = [Decimal("0.004")]
    earlier = fake.run_default(
        "POST",
        "/fapi/v1/order",
        {"symbol": SYMBOL, "side": "BUY", "type": "MARKET", "quantity": "0.010", "newClientOrderId": EN,
         "newOrderRespType": "RESULT"},
    )
    assert earlier["status"] == "EXPIRED" and earlier["executedQty"] == "0.004"
    out = open_with(broker, make_plan(tp=None))
    assert not fake.calls_to("POST", "/fapi/v1/order")  # never re-sent
    assert out.filled and out.message == "reused"
    assert out.qty == pytest.approx(0.004)
    assert out.entry_order is not None
    assert out.entry_order.status is OrderStatus.EXPIRED and out.entry_order.exchange_id == str(earlier["orderId"])
    assert out.protective[0].client_id == SL1

    # an earlier attempt that ended with nothing executed -> a fresh id is used
    broker.close_position(SYMBOL, None, reason=ExitReason.SIGNAL, client_id=cid("EX", BAR), ref_price=None, bar_time=ENTRY_BAR)
    en2 = cid("EN", BAR + H)
    fake.fill_limits = [Decimal("0")]
    fake.run_default(
        "POST",
        "/fapi/v1/order",
        {"symbol": SYMBOL, "side": "BUY", "type": "MARKET", "quantity": "0.010", "newClientOrderId": en2},
    )
    fake.calls.clear()
    out2 = open_with(broker, make_plan(tp=None), en=en2, sl=cid("SL", ENTRY_BAR + H, 1))
    assert out2.filled
    assert fake.calls_to("POST", "/fapi/v1/order")[0].params["newClientOrderId"] == next_client_id(en2)


def test_avg_price_missing_falls_back_to_get_order(fake: FakeBinance, make_broker: Callable[..., ExchangeBroker]) -> None:
    broker = make_broker(prepare=True)
    fake.fill_price = Decimal("84012.3")

    def without_avg(p: dict[str, str]) -> Any:
        resp = fake.run_default("POST", "/fapi/v1/order", p)
        return {k: v for k, v in resp.items() if k not in ("avgPrice", "cumQuote")}

    fake.script("POST", "/fapi/v1/order", without_avg)
    out = open_with(broker, make_plan(tp=None))
    assert out.avg_price == pytest.approx(84_012.3)
    by_id = [c for c in fake.calls_to("GET", "/fapi/v1/order") if c.params.get("orderId") == "1001"]
    assert len(by_id) == 1
    assert fake.idx("POST", "/fapi/v1/order") < fake.calls.index(by_id[0])


# ---------------------------------------------------------------------------------------------
# close_position
# ---------------------------------------------------------------------------------------------


def test_close_position_reduce_only_and_cancels_only_own_orders(
    fake: FakeBinance, make_broker: Callable[..., ExchangeBroker], clock: Any
) -> None:
    broker = make_broker(prepare=True)
    plan = make_plan()
    out = open_with(broker, plan)
    active = active_from(plan, out)
    own_leftover = cid("EN", BAR - 4 * H)
    fake.add_open_order(own_leftover)  # own regular order (e.g. left by a crash)
    fake.add_open_order("ios_xyz", side="SELL", price="99000")  # foreign regular order
    foreign_algo = fake.add_algo("web_abc123", "STOP_MARKET", "SELL", "70000.0")
    clock.advance(3600)
    fake.add_income("FUNDING_FEE", "-0.12", time=out.entry_time + 1_000)
    fake.add_income("FUNDING_FEE", "0.02", time=out.entry_time + 2_000)
    fake.fill_price = Decimal("85000")
    fake.calls.clear()

    ex = cid("EX", BAR + H)
    closure = broker.close_position(SYMBOL, active, reason=ExitReason.SIGNAL, client_id=ex, ref_price=85_000.0, bar_time=ENTRY_BAR + H)
    assert fake.calls_to("POST", "/fapi/v1/order")[0].items == (
        ("symbol", SYMBOL),
        ("side", "SELL"),
        ("type", "MARKET"),
        ("quantity", "0.01"),
        ("reduceOnly", "true"),
        ("newClientOrderId", ex),
        ("newOrderRespType", "RESULT"),
    )
    assert [c.params for c in fake.calls_to("DELETE", "/fapi/v1/algoOrder")] == [
        {"clientAlgoId": SL1},
        {"clientAlgoId": TP1},
    ]
    assert [c.params for c in fake.calls_to("DELETE", "/fapi/v1/order")] == [
        {"symbol": SYMBOL, "origClientOrderId": own_leftover}
    ]
    assert not [c for c in fake.calls if c.path in {"/fapi/v1/allOpenOrders", "/fapi/v1/algoOpenOrders"}]
    assert foreign_algo["algoStatus"] == "NEW"
    assert next(o for o in fake.orders if o["clientOrderId"] == "ios_xyz")["status"] == "NEW"
    assert fake.position_amt == 0

    assert closure is not None
    assert closure.reason is ExitReason.SIGNAL
    assert closure.qty == pytest.approx(0.01)
    assert closure.exit_price == pytest.approx(85_000.0)
    assert closure.exit_fee == pytest.approx(0.01 * 85_000.0 * 0.0005)
    assert closure.gross_pnl == pytest.approx(10.0)  # realizedPnl
    assert closure.funding == pytest.approx(0.10)  # -(-0.12 + 0.02): paid
    assert closure.exit_time == fake.trades[-1]["time"]
    assert closure.order is not None
    assert closure.order.client_id == ex and closure.order.purpose is OrderPurpose.EXIT
    assert closure.order.fee == pytest.approx(0.425)
    income = fake.calls_to("GET", "/fapi/v1/income")[0]
    assert income.params["startTime"] == str(active.entry_time) and income.params["incomeType"] == "FUNDING_FEE"

    # already flat -> None, own orphans still cancelled, foreign untouched
    orphan = fake.add_algo(cid("TP", ENTRY_BAR, 3), "TAKE_PROFIT_MARKET", "SELL", "88000.0")
    assert broker.close_position(SYMBOL, None, reason=ExitReason.SIGNAL, client_id=cid("EX", BAR + 2 * H), ref_price=None, bar_time=ENTRY_BAR) is None
    assert orphan["algoStatus"] == "CANCELED" and foreign_algo["algoStatus"] == "NEW"


def test_close_partial_fill_retries_with_new_id(fake: FakeBinance, make_broker: Callable[..., ExchangeBroker]) -> None:
    broker = make_broker(prepare=True)
    fake.set_position("0.010", "84000")
    fake.fill_limits = [Decimal("0.004")]  # the first close order EXPIRES after a partial fill
    fake.fill_prices = [Decimal("85000"), Decimal("84900")]
    fl0 = make_client_id(BOT, SYMBOL, "FL", ENTRY_BAR, 0)
    closure = broker.close_position(
        SYMBOL, make_active(), reason=ExitReason.PROTECTION_FAILED, client_id=fl0, ref_price=None, bar_time=ENTRY_BAR
    )
    posts = fake.calls_to("POST", "/fapi/v1/order")
    assert [(c.params["newClientOrderId"], c.params["quantity"]) for c in posts] == [
        (fl0, "0.01"),
        (next_client_id(fl0), "0.006"),  # the remaining quantity, with a never-used id
    ]
    assert fake.find_order(1001)["status"] == "EXPIRED"
    assert fake.position_amt == 0
    assert closure is not None
    assert closure.qty == pytest.approx(0.01)
    assert closure.exit_price == pytest.approx((0.004 * 85_000 + 0.006 * 84_900) / 0.01)
    assert closure.gross_pnl == pytest.approx(0.004 * 1_000 + 0.006 * 900)
    assert closure.order is not None and closure.order.client_id == next_client_id(fl0)
    assert closure.reason is ExitReason.PROTECTION_FAILED


def test_close_duplicate_client_id_uses_next_id(fake: FakeBinance, make_broker: Callable[..., ExchangeBroker]) -> None:
    broker = make_broker(prepare=True)
    fake.set_position("0.010", "84000")
    ex = cid("EX", BAR)
    fake.script("POST", "/fapi/v1/order", err(-4116, "ClientOrderId is duplicated."))
    closure = broker.close_position(SYMBOL, make_active(), reason=ExitReason.SIGNAL, client_id=ex, ref_price=None, bar_time=ENTRY_BAR)
    assert [c.params["newClientOrderId"] for c in fake.calls_to("POST", "/fapi/v1/order")] == [ex, next_client_id(ex)]
    assert fake.position_amt == 0
    assert closure is not None and closure.order is not None and closure.order.client_id == next_client_id(ex)

    # ids already used on the exchange are skipped BEFORE sending (lookups are then unambiguous)
    fake.set_position("0.010", "84000")
    fake.calls.clear()
    closure = broker.close_position(SYMBOL, make_active(), reason=ExitReason.SIGNAL, client_id=ex, ref_price=None, bar_time=ENTRY_BAR)
    assert fake.calls_to("POST", "/fapi/v1/order")[0].params["newClientOrderId"] == next_client_id(next_client_id(ex))


def test_close_unknown_outcome_resolved_by_lookup(fake: FakeBinance, make_broker: Callable[..., ExchangeBroker]) -> None:
    broker = make_broker(prepare=True)
    fake.set_position("0.010", "84000")

    def filled_but_unknown(p: dict[str, str]) -> Reply:
        fake.run_default("POST", "/fapi/v1/order", p)
        return UNKNOWN_503

    fake.script("POST", "/fapi/v1/order", filled_but_unknown)
    ks = cid("KS", BAR)
    closure = broker.close_position(SYMBOL, make_active(), reason=ExitReason.KILL_SWITCH, client_id=ks, ref_price=None, bar_time=ENTRY_BAR)
    assert len(fake.calls_to("POST", "/fapi/v1/order")) == 1
    assert closure is not None and closure.order is not None and closure.order.client_id == ks
    assert closure.order.purpose is OrderPurpose.FLATTEN
    assert closure.qty == pytest.approx(0.01)


def test_funding_income_paginates(fake: FakeBinance, make_broker: Callable[..., ExchangeBroker]) -> None:
    broker = make_broker(prepare=True)
    start = 1_790_000_000_000
    end = start + 5_000_000
    for i in range(1000):
        fake.add_income("FUNDING_FEE", "-0.01", time=start + i * 1_000)
    for i in range(3):
        fake.add_income("FUNDING_FEE", "0.02", time=start + 1_500_000 + i)
    fake.add_income("COMMISSION", "-5", time=start + 10)  # other income types are not funding
    assert broker._funding_income(SYMBOL, start, end) == pytest.approx(10.0 - 0.06)
    calls = fake.calls_to("GET", "/fapi/v1/income")
    assert len(calls) == 2
    assert calls[0].items == (
        ("symbol", SYMBOL),
        ("incomeType", "FUNDING_FEE"),
        ("startTime", str(start)),
        ("endTime", str(end)),
        ("limit", "1000"),
    )
    assert calls[1].params["startTime"] == str(start + 999 * 1_000 + 1)  # last row time + 1


# ---------------------------------------------------------------------------------------------
# sync
# ---------------------------------------------------------------------------------------------


def test_sync_detects_stop_loss_closure(
    fake: FakeBinance, make_broker: Callable[..., ExchangeBroker], clock: Any
) -> None:
    broker = make_broker(prepare=True)
    plan = make_plan()
    out = open_with(broker, plan)
    active = active_from(plan, out)
    clock.advance(1800)
    fired = fake.trigger_algo(SL1, "81990")  # the stop fired on the exchange (its order id is not ours)
    fake.add_income("FUNDING_FEE", "-0.05", time=out.entry_time + 10)
    fake.calls.clear()

    res = broker.sync(SYMBOL, active, [])
    c = res.closure
    assert c is not None
    assert c.reason is ExitReason.STOP_LOSS
    assert c.qty == pytest.approx(0.01)
    assert c.exit_price == pytest.approx(81_990.0)
    assert c.exit_fee == pytest.approx(0.01 * 81_990 * 0.0005)
    assert c.gross_pnl == pytest.approx(0.01 * (81_990 - 84_000))
    assert c.funding == pytest.approx(0.05)
    assert c.exit_time == fired["updateTime"]
    assert res.issues == ("ORPHAN_PROTECTIVE_CANCELED",)  # the own TP was still open
    assert fake.algo(TP1)["algoStatus"] == "CANCELED"
    assert res.account.position is None and res.account.protective_orders == ()
    assert len(fake.calls_to("GET", "/fapi/v3/account")) == 2  # re-read after cancelling
    trades = fake.calls_to("GET", "/fapi/v1/userTrades")[0]
    assert trades.items == (("symbol", SYMBOL), ("startTime", str(active.entry_time)), ("limit", "1000"))
    assert fake.calls_to("GET", "/fapi/v1/allAlgoOrders")[0].params == {"symbol": SYMBOL, "startTime": str(active.entry_time)}


def test_sync_closure_without_entry_order_id_uses_time_filter(
    fake: FakeBinance, make_broker: Callable[..., ExchangeBroker], clock: Any
) -> None:
    broker = make_broker(prepare=True)
    adopted_at = fake.now
    fake.set_position("0.010", "84000", update_time=adopted_at)
    active = make_adopted(qty=0.010, entry_price=84_000.0, updated_at=adopted_at)
    fake.add_trade(700, "SELL", "80000", "0.5", time=adopted_at - 60_000, realized="-99")  # before the adoption
    clock.advance(600)
    fake.fill_price = Decimal("84500")
    fake.run_default(  # closed manually in the web UI
        "POST",
        "/fapi/v1/order",
        {"symbol": SYMBOL, "side": "SELL", "type": "MARKET", "quantity": "0.010", "reduceOnly": "true",
         "newClientOrderId": "web_manual_1"},
    )
    fake.script("GET", "/fapi/v1/userTrades", lambda p: list(fake.trades))  # the exchange returns older rows too

    res = broker.sync(SYMBOL, active, [])
    c = res.closure
    assert c is not None
    assert c.reason is ExitReason.MANUAL
    assert c.qty == pytest.approx(0.01)  # the pre-adoption fill is ignored
    assert c.exit_price == pytest.approx(84_500.0)
    assert c.gross_pnl == pytest.approx(5.0)
    assert "CLOSURE_DETAILS_UNKNOWN" not in res.issues


def test_sync_liquidation_adds_insurance_clear_fee(
    fake: FakeBinance, make_broker: Callable[..., ExchangeBroker], clock: Any
) -> None:
    broker = make_broker(prepare=True)
    plan = make_plan()
    out = open_with(broker, plan)
    active = active_from(plan, out)
    clock.advance(7200)
    fake.fill_price = Decimal("56500")
    fake.run_default(
        "POST",
        "/fapi/v1/order",
        {"symbol": SYMBOL, "side": "SELL", "type": "MARKET", "quantity": "0.010", "reduceOnly": "true",
         "newClientOrderId": "autoclose-1790776800123"},
    )
    for a in fake.algos:
        a["algoStatus"] = "EXPIRED"  # close-position orders expire once the position is gone
    fake.add_income("INSURANCE_CLEAR", "-3.5", time=fake.now)
    fake.calls.clear()

    res = broker.sync(SYMBOL, active, [])
    c = res.closure
    assert c is not None
    assert c.reason is ExitReason.LIQUIDATION
    assert c.exit_price == pytest.approx(56_500.0)
    assert c.gross_pnl == pytest.approx(0.01 * (56_500 - 84_000))
    assert c.exit_fee == pytest.approx(0.01 * 56_500 * 0.0005 + 3.5)
    clear = [c for c in fake.calls_to("GET", "/fapi/v1/income") if c.params["incomeType"] == "INSURANCE_CLEAR"]
    assert len(clear) == 1 and clear[0].params["startTime"] == str(active.entry_time)
    assert res.issues == ()


def test_sync_closure_details_unknown_without_fills(fake: FakeBinance, make_broker: Callable[..., ExchangeBroker]) -> None:
    broker = make_broker(prepare=True)
    active = make_active()  # the exchange is flat and has no fills for it
    res = broker.sync(SYMBOL, active, [])
    assert res.issues == ("CLOSURE_DETAILS_UNKNOWN",)
    assert res.closure is not None
    assert res.closure.reason is ExitReason.UNKNOWN
    assert res.closure.exit_price == active.entry_price and res.closure.gross_pnl is None


def test_sync_reports_untracked_position(fake: FakeBinance, make_broker: Callable[..., ExchangeBroker]) -> None:
    broker = make_broker(prepare=True)
    fake.set_position("-0.020", "85000")
    res = broker.sync(SYMBOL, None, [])
    assert "UNTRACKED_POSITION" in res.issues and "SL_MISSING" in res.issues
    pos = res.account.position
    assert pos is not None
    assert pos.qty == pytest.approx(-0.02) and pos.direction is Direction.SHORT
    assert pos.entry_price == 85_000.0 and pos.leverage == 3
    assert pos.updated_at == fake.position_update_time
    assert res.account.equity == pytest.approx(10_000.0)
    assert res.account.wallet_balance == pytest.approx(10_000.0)
    assert res.closure is None
    assert not [c for c in fake.calls if c.method != "GET"]


def test_sync_ignores_foreign_stop_for_sl_missing(fake: FakeBinance, make_broker: Callable[..., ExchangeBroker]) -> None:
    broker = make_broker(prepare=True)
    fake.wrap_open_algo = True  # openAlgoOrders may answer {"orders": [...]}
    fake.set_position("0.010", "84000")
    fake.add_algo("web_sl_1", "STOP_MARKET", "SELL", "82000.0")
    fake.add_algo(SL1, "STOP_MARKET", "SELL", "82000.0", status="CANCELED")  # own but no longer live
    res = broker.sync(SYMBOL, make_active(), [])
    assert "SL_MISSING" in res.issues
    assert "FOREIGN_OPEN_ORDERS" in res.issues
    assert [p.client_id for p in res.account.protective_orders] == ["web_sl_1"]  # shown, never cancelled
    assert not fake.calls_to("DELETE", "/fapi/v1/algoOrder")
    # with a live own stop the position is fine
    fake.add_algo(SL2, "STOP_MARKET", "SELL", "82000.0")
    assert broker.sync(SYMBOL, make_active(), []).issues == ("FOREIGN_OPEN_ORDERS",)


def test_sync_reduce_only_small_sl_qty_reports_mismatch(
    fake: FakeBinance, make_broker: Callable[..., ExchangeBroker]
) -> None:
    broker = make_broker(protective_mode="reduce_only", prepare=True)
    fake.set_position("0.010", "84000")
    small = fake.add_algo(SL1, "STOP_MARKET", "SELL", "82000.0", close_position=False, quantity="0.004")
    res = broker.sync(SYMBOL, make_active(take_profit=None), [])
    assert res.issues == ("PROTECTION_QTY_MISMATCH",)
    sl = res.account.protective_orders[0]
    assert sl.quantity == pytest.approx(0.004) and sl.close_position is False

    small["quantity"] = "0.010"
    assert broker.sync(SYMBOL, make_active(take_profit=None), []).issues == ()
    fake.add_algo(TP1, "TAKE_PROFIT_MARKET", "SELL", "88000.0", close_position=False, quantity="0.004")
    assert broker.sync(SYMBOL, make_active(), []).issues == ("PROTECTION_QTY_MISMATCH",)


def test_sync_reports_qty_mismatch(fake: FakeBinance, make_broker: Callable[..., ExchangeBroker]) -> None:
    broker = make_broker(prepare=True)
    fake.set_position("0.012", "84000")
    fake.add_algo(SL1, "STOP_MARKET", "SELL", "82000.0")
    assert broker.sync(SYMBOL, make_active(qty=0.010), []).issues == ("QTY_MISMATCH",)


def test_sync_cancels_orphan_own_algo_orders_when_flat(
    fake: FakeBinance, make_broker: Callable[..., ExchangeBroker]
) -> None:
    broker = make_broker(prepare=True)
    own = fake.add_algo(SL1, "STOP_MARKET", "SELL", "82000.0")
    foreign = fake.add_algo("web_tp", "TAKE_PROFIT_MARKET", "SELL", "90000.0")
    res = broker.sync(SYMBOL, None, [])
    assert [c.params for c in fake.calls_to("DELETE", "/fapi/v1/algoOrder")] == [{"clientAlgoId": SL1}]
    assert own["algoStatus"] == "CANCELED" and foreign["algoStatus"] == "NEW"
    assert res.issues == ("ORPHAN_PROTECTIVE_CANCELED", "FOREIGN_OPEN_ORDERS")
    assert len(fake.calls_to("GET", "/fapi/v3/account")) == 2  # re-read after cancelling
    assert [p.client_id for p in res.account.protective_orders] == ["web_tp"]
    assert res.closure is None


# ---------------------------------------------------------------------------------------------
# ensure_protection
# ---------------------------------------------------------------------------------------------


def test_ensure_protection_replaces_missing_sl(fake: FakeBinance, make_broker: Callable[..., ExchangeBroker]) -> None:
    broker = make_broker(prepare=True)
    fake.set_position("0.010", "84000")
    fake.add_algo(TP1, "TAKE_PROFIT_MARKET", "SELL", "88000.0")  # the SL was cancelled by hand; the TP is fine
    active = make_active()
    res = broker.sync(SYMBOL, active, [])
    assert res.issues == ("SL_MISSING",)
    fake.calls.clear()

    placed = broker.ensure_protection(active, res.account, sl_client_id=SL2, tp_client_id=TP2)
    posts = fake.calls_to("POST", "/fapi/v1/algoOrder")
    assert [c.params for c in posts] == [
        {
            "algoType": "CONDITIONAL",
            "symbol": SYMBOL,
            "side": "SELL",
            "type": "STOP_MARKET",
            "triggerPrice": "82000",
            "workingType": "MARK_PRICE",
            "priceProtect": "false",
            "closePosition": "true",
            "clientAlgoId": SL2,
        }
    ]
    assert not fake.calls_to("DELETE", "/fapi/v1/algoOrder")
    assert [(p.kind, p.client_id) for p in placed] == [(OrderPurpose.STOP_LOSS, SL2), (OrderPurpose.TAKE_PROFIT, TP1)]

    # everything valid -> no request at all
    acct = broker.sync(SYMBOL, active, []).account
    fake.calls.clear()
    again = broker.ensure_protection(active, acct, sl_client_id=cid("SL", ENTRY_BAR, 3), tp_client_id=cid("TP", ENTRY_BAR, 3))
    assert fake.calls == []
    assert {p.client_id for p in again} == {SL2, TP1}


def test_ensure_protection_places_before_cancel(fake: FakeBinance, make_broker: Callable[..., ExchangeBroker]) -> None:
    broker = make_broker(protective_mode="reduce_only", prepare=True)
    fake.set_position("0.010", "84000")
    stale = fake.add_algo(SL1, "STOP_MARKET", "SELL", "82000.0", close_position=False, quantity="0.004")
    active = make_active(take_profit=None)
    res = broker.sync(SYMBOL, active, [])
    assert res.issues == ("PROTECTION_QTY_MISMATCH",)
    fake.calls.clear()

    placed = broker.ensure_protection(active, res.account, sl_client_id=SL2, tp_client_id=None)
    order = [(c.method, c.path, c.params.get("clientAlgoId")) for c in fake.calls if c.path == "/fapi/v1/algoOrder"]
    assert order == [("POST", "/fapi/v1/algoOrder", SL2), ("DELETE", "/fapi/v1/algoOrder", SL1)]
    assert fake.calls_to("POST", "/fapi/v1/algoOrder")[0].params["quantity"] == "0.01"
    assert stale["algoStatus"] == "CANCELED" and fake.algo(SL2)["algoStatus"] == "NEW"
    assert [p.client_id for p in placed] == [SL2]


def test_ensure_protection_sl_failure_flattens(fake: FakeBinance, make_broker: Callable[..., ExchangeBroker]) -> None:
    broker = make_broker(prepare=True)
    fake.set_position("0.010", "84000")
    active = make_active()
    acct = broker.sync(SYMBOL, active, []).account
    fake.script("POST", "/fapi/v1/algoOrder", err(-2021, "Order would immediately trigger."))
    with pytest.raises(ProtectionFailedError) as info:
        broker.ensure_protection(active, acct, sl_client_id=SL2, tp_client_id=TP2)
    assert info.value.entry is None and info.value.flattened
    assert info.value.closure is not None and info.value.closure.reason is ExitReason.PROTECTION_FAILED
    fl = make_client_id(BOT, SYMBOL, "FL", ENTRY_BAR, active.protect_seq + 1)
    assert fake.calls_to("POST", "/fapi/v1/order")[0].params["newClientOrderId"] == fl
    assert fake.position_amt == 0
