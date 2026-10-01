"""Risk management: stops, sizing, signal -> action mapping, daily-loss kill switch, cooldown (SPEC §8.5).

Shared by the backtest engine and the trader, so both size and gate entries identically.
Floats are used for the arithmetic; exchange precision (tick/step) is applied with ``bot.exchange.filters``.
"""

from __future__ import annotations

import logging
import math
from collections.abc import Mapping
from decimal import Decimal
from typing import Any, Final

from bot.config import FeeConfig, RiskConfig, StopLossConfig
from bot.errors import ConfigError
from bot.exchange.filters import meets_min_notional, normalize_market_qty, round_protective_price, to_decimal
from bot.models import Action, Direction, RiskDecision, SignalAction, SymbolFilters, TradePlan
from bot.timeutil import utc_day

logger = logging.getLogger(__name__)

# plan_entry rejection codes (RiskDecision.reason; the trader reports them as "risk:<code>").
REASON_OK: Final = "ok"
REJECT_NO_EQUITY: Final = "no_equity"
REJECT_BAD_PRICE: Final = "bad_price"
REJECT_STOP_UNAVAILABLE: Final = "stop_unavailable"
REJECT_INVALID_STOP: Final = "invalid_stop"
REJECT_INVALID_TAKE_PROFIT: Final = "invalid_take_profit"
REJECT_BELOW_MIN_QTY: Final = "below_min_qty"
REJECT_BELOW_MIN_NOTIONAL: Final = "below_min_notional"
REJECT_LIQUIDATION_TOO_CLOSE: Final = "liquidation_too_close"
REJECTION_CODES: Final[frozenset[str]] = frozenset(
    {
        REJECT_NO_EQUITY,
        REJECT_BAD_PRICE,
        REJECT_STOP_UNAVAILABLE,
        REJECT_INVALID_STOP,
        REJECT_INVALID_TAKE_PROFIT,
        REJECT_BELOW_MIN_QTY,
        REJECT_BELOW_MIN_NOTIONAL,
        REJECT_LIQUIDATION_TOO_CLOSE,
    }
)

SIZING_CAPS: Final[tuple[str, ...]] = ("risk", "margin", "notional")  # tie-break order of plan_entry step 8


def _require_trade_direction(direction: Direction) -> Direction:
    d = Direction(direction)
    if d is Direction.FLAT:
        raise ValueError("direction must be LONG or SHORT, not FLAT")
    return d


def _finite_positive(x: float | None) -> bool:
    return x is not None and math.isfinite(x) and x > 0


# ---------------------------------------------------------------------------------------------
# Stop / take-profit / liquidation
# ---------------------------------------------------------------------------------------------


def compute_stop_price(entry: float, direction: Direction, cfg: StopLossConfig, atr_value: float | None) -> float | None:
    """Raw (unrounded) stop price, or None when it cannot be computed.

    percent: LONG ``entry*(1-p/100)``, SHORT ``entry*(1+p/100)``;
    atr: None if ``atr_value`` is None/NaN/<=0, else LONG ``entry - m*atr``, SHORT ``entry + m*atr``.
    Returns None if the result is <= 0 (or not finite).
    """
    d = _require_trade_direction(direction)
    entry_f = float(entry)
    if cfg.mode == "percent":
        p = float(cfg.percent) / 100.0
        stop = entry_f * (1.0 - p) if d is Direction.LONG else entry_f * (1.0 + p)
    elif cfg.mode == "atr":
        if atr_value is None:
            return None
        a = float(atr_value)
        if not _finite_positive(a):
            return None
        dist = float(cfg.atr_multiple) * a
        stop = entry_f - dist if d is Direction.LONG else entry_f + dist
    else:
        raise ConfigError(f"unknown risk.stop_loss.mode {cfg.mode!r}; expected percent or atr")
    return stop if _finite_positive(stop) else None


def compute_take_profit(entry: float, stop: float, direction: Direction, r_multiple: float | None) -> float | None:
    """``entry + sign * r * |entry - stop|``; None if ``r_multiple`` is None (no take-profit order)."""
    d = _require_trade_direction(direction)
    if r_multiple is None:
        return None
    entry_f = float(entry)
    return entry_f + d.sign * float(r_multiple) * abs(entry_f - float(stop))


def approx_liquidation_price(entry: float, direction: Direction, leverage: int, mmr: float) -> float:
    """Conservative isolated-margin liquidation price (cum = 0).

    LONG ``entry*(1 - 1/L)/(1 - mmr)``; SHORT ``entry*(1 + 1/L)/(1 + mmr)``.
    e.g. (84000, LONG, 10, 0.004) -> 75903.6145; (84000, SHORT, 10, 0.004) -> 92031.8725.
    """
    d = _require_trade_direction(direction)
    lev = float(leverage)
    if not lev >= 1:
        raise ValueError(f"leverage must be >= 1, got {leverage!r}")
    m = float(mmr)
    if not 0 <= m < 1:
        raise ValueError(f"maintenance margin rate must be in [0, 1), got {mmr!r}")
    entry_f = float(entry)
    if d is Direction.LONG:
        return entry_f * (1.0 - 1.0 / lev) / (1.0 - m)
    return entry_f * (1.0 + 1.0 / lev) / (1.0 + m)


def stop_out_loss_per_unit(
    direction: Direction, ref_price: float, stop_price: float, taker_fee: float, slippage_bps: float
) -> float:
    """USDT lost per unit on a clean stop-out, exactly as the fill model books it (§9.1).

    Entry and SL exit both slip adversely and pay the taker fee on the slipped prices:
      LONG : (ref - s) + ref*slip + s*slip + ref*(1+slip)*taker + s*(1-slip)*taker
      SHORT: (s - ref) + ref*slip + s*slip + ref*(1-slip)*taker + s*(1+slip)*taker
    """
    d = _require_trade_direction(direction)
    ref = float(ref_price)
    s = float(stop_price)
    slip = float(slippage_bps) / 10_000
    taker = float(taker_fee)
    if d is Direction.LONG:
        return (ref - s) + ref * slip + s * slip + ref * (1 + slip) * taker + s * (1 - slip) * taker
    return (s - ref) + ref * slip + s * slip + ref * (1 - slip) * taker + s * (1 + slip) * taker


# ---------------------------------------------------------------------------------------------
# Entry planning (position sizing)
# ---------------------------------------------------------------------------------------------


def plan_entry(
    *,
    direction: Direction,
    ref_price: float,
    equity: float,
    atr_value: float | None,
    filters: SymbolFilters,
    risk: RiskConfig,
    fees: FeeConfig,
    slippage_bps: float,
) -> RiskDecision:
    """Size an entry (SPEC §8.5, exact order; the first failing check returns ``RiskDecision(None, code)``).

    ``risk.max_position_notional`` is expected to already be capped by the exchange bracket (the trader passes
    ``_effective_risk()``).
    """
    # 1. inputs
    d = _require_trade_direction(direction)
    equity_f = float(equity)
    ref = float(ref_price)
    if not _finite_positive(equity_f):
        return RiskDecision(None, REJECT_NO_EQUITY)
    if not _finite_positive(ref):
        return RiskDecision(None, REJECT_BAD_PRICE)

    # 2. raw stop
    stop_raw = compute_stop_price(ref, d, risk.stop_loss, atr_value)
    if stop_raw is None:
        return RiskDecision(None, REJECT_STOP_UNAVAILABLE)

    # 3. tick-rounded stop, rounded toward the entry (tighter), must stay on the loss side
    stop: Decimal = round_protective_price(stop_raw, filters.tick_size, entry=ref)
    ref_dec = to_decimal(ref)
    if (d is Direction.LONG and not stop < ref_dec) or (d is Direction.SHORT and not stop > ref_dec) or stop <= 0:
        return RiskDecision(None, REJECT_INVALID_STOP)
    s = float(stop)

    # 4. take-profit (optional), rounded toward the entry, must stay on the profit side
    tp: Decimal | None = None
    tp_raw = compute_take_profit(ref, s, d, risk.take_profit_r)
    if tp_raw is not None:
        # SPEC-GAP: a non-positive / non-finite TP (short with a huge R multiple) cannot be sent -> invalid.
        if not _finite_positive(tp_raw):
            return RiskDecision(None, REJECT_INVALID_TAKE_PROFIT)
        tp = round_protective_price(tp_raw, filters.tick_size, entry=ref)
        wrong_side = (d is Direction.LONG and not tp > ref_dec) or (d is Direction.SHORT and not tp < ref_dec)
        if wrong_side or tp <= 0:
            return RiskDecision(None, REJECT_INVALID_TAKE_PROFIT)

    # 5. per-unit loss of a clean stop-out, slippage- and fee-exact
    per_unit_loss = stop_out_loss_per_unit(d, ref, s, fees.taker, slippage_bps)
    if not _finite_positive(per_unit_loss):
        return RiskDecision(None, REJECT_INVALID_STOP)

    # 6.-8. size = min(risk, margin, notional) bounds, floored to the market step
    risk_amount_target = equity_f * float(risk.risk_per_trade_pct) / 100.0
    qty_risk = risk_amount_target / per_unit_loss
    qty_margin = equity_f * float(risk.max_margin_fraction) * float(risk.leverage) / ref
    qty_notional = float(risk.max_position_notional) / ref
    # min() keeps the FIRST minimal item -> ties resolve in the order risk, margin, notional.
    sizing_cap, raw_qty = min(zip(SIZING_CAPS, (qty_risk, qty_margin, qty_notional)), key=lambda kv: kv[1])
    if not (math.isfinite(raw_qty) and raw_qty > 0):
        return RiskDecision(None, REJECT_BELOW_MIN_QTY)
    qty = normalize_market_qty(raw_qty, filters)
    if qty <= 0:
        return RiskDecision(None, REJECT_BELOW_MIN_QTY)
    if not meets_min_notional(qty, ref, filters):
        return RiskDecision(None, REJECT_BELOW_MIN_NOTIONAL)  # never size up to reach the minimum

    # 9. the stop must sit well inside the (conservative) liquidation price
    liq = approx_liquidation_price(ref, d, risk.leverage, float(risk.maint_margin_rate) + float(risk.liq_mmr_buffer))
    if abs(ref - liq) < float(risk.min_liq_distance_multiple) * abs(ref - s):
        return RiskDecision(None, REJECT_LIQUIDATION_TOO_CLOSE)

    # 10. plan
    qty_f = float(qty)
    plan = TradePlan(
        symbol=filters.symbol,
        direction=d,
        ref_price=ref,
        qty=qty,
        stop_price=stop,
        take_profit_price=tp,
        notional=qty_f * ref,
        risk_amount=qty_f * per_unit_loss,
        leverage=int(risk.leverage),
        liquidation_price=float(liq),
        sizing_cap=sizing_cap,
    )
    return RiskDecision(plan, REASON_OK)


# ---------------------------------------------------------------------------------------------
# Signal -> action
# ---------------------------------------------------------------------------------------------


def decide_action(signal: SignalAction, position: Direction, entries_allowed: bool) -> Action:
    """Map a strategy signal and the current position to an action (SPEC §8.5 table).

    | signal \\ position | FLAT                         | LONG                          | SHORT                        |
    | NONE               | NONE                         | NONE                          | NONE                         |
    | LONG               | OPEN_LONG if allowed else NONE | NONE                        | FLIP_LONG if allowed else CLOSE |
    | SHORT              | OPEN_SHORT if allowed else NONE | FLIP_SHORT if allowed else CLOSE | NONE                    |
    | CLOSE              | NONE                         | CLOSE                         | NONE                         |

    CLOSE means "exit a long" (dead cross with shorts disabled); it never closes a short. Exits are never blocked.
    """
    sig = SignalAction(signal)
    pos = Direction(position)
    allowed = bool(entries_allowed)
    if sig is SignalAction.NONE:
        return Action.NONE
    if sig is SignalAction.CLOSE:
        return Action.CLOSE if pos is Direction.LONG else Action.NONE
    if sig is SignalAction.LONG:
        if pos is Direction.FLAT:
            return Action.OPEN_LONG if allowed else Action.NONE
        if pos is Direction.SHORT:
            return Action.FLIP_LONG if allowed else Action.CLOSE
        return Action.NONE
    # SignalAction.SHORT
    if pos is Direction.FLAT:
        return Action.OPEN_SHORT if allowed else Action.NONE
    if pos is Direction.LONG:
        return Action.FLIP_SHORT if allowed else Action.CLOSE
    return Action.NONE


# ---------------------------------------------------------------------------------------------
# Daily loss kill switch
# ---------------------------------------------------------------------------------------------


def _opt_float(v: Any) -> float | None:
    return None if v is None else float(v)


def _opt_int(v: Any) -> int | None:
    return None if v is None else int(v)


class DailyLossKillSwitch:
    """Blocks entries for the rest of the UTC day once equity fell ``max_daily_loss_pct`` % below the baseline.

    The baseline of day D is the last equity seen before D (the close of the previous day's last bar, or the
    seeded starting equity), so the first bar of every day counts and on 1d candles every bar can trip.
    Re-arms at 00:00 UTC (09:00 KST). ``max_daily_loss_pct == 0`` disables it.
    """

    def __init__(self, max_daily_loss_pct: float) -> None:
        pct = float(max_daily_loss_pct)
        if not (math.isfinite(pct) and 0 <= pct < 100):
            raise ValueError(f"max_daily_loss_pct must be in [0, 100), got {max_daily_loss_pct!r}")
        self.max_daily_loss_pct: float = pct
        self.day: str | None = None
        self.day_start_equity: float | None = None
        self.last_equity: float | None = None
        self.tripped: bool = False
        self.tripped_at: int | None = None
        self.reason: str | None = None

    def seed(self, equity: float) -> None:
        """Set the starting baseline once (backtest: initial balance; trader: equity at startup)."""
        if self.last_equity is None:
            self.last_equity = self._check_equity(equity)

    def update(self, t_ms: int, equity: float) -> bool:
        """Feed the closing equity of the bar whose CLOSE time is ``t_ms``. Returns True only when newly tripped."""
        eq = self._check_equity(equity)
        t = int(t_ms)
        d = utc_day(t)
        if d != self.day:  # first call, or a new UTC day
            self.day = d
            self.day_start_equity = self.last_equity if self.last_equity is not None else eq
            self.tripped = False
            self.tripped_at = None
            self.reason = None
        self.last_equity = eq
        pct = self.max_daily_loss_pct
        base = self.day_start_equity
        if pct > 0 and not self.tripped and base is not None and eq <= base * (1 - pct / 100):
            loss_pct = (1 - eq / base) * 100 if base > 0 else 100.0
            self.tripped = True
            self.tripped_at = t
            self.reason = f"daily loss {loss_pct:.2f}% >= {pct}%"
            logger.warning(
                "daily loss kill switch tripped at %d: equity %.2f vs day start %.2f (%s)", t, eq, base, self.reason
            )
            return True
        return False

    @property
    def entries_allowed(self) -> bool:
        return not self.tripped

    def to_dict(self) -> dict[str, Any]:
        return {
            "max_daily_loss_pct": self.max_daily_loss_pct,
            "day": self.day,
            "day_start_equity": self.day_start_equity,
            "last_equity": self.last_equity,
            "tripped": self.tripped,
            "tripped_at": self.tripped_at,
            "reason": self.reason,
        }

    @classmethod
    def from_dict(cls, d: Mapping[str, Any], max_daily_loss_pct: float) -> DailyLossKillSwitch:
        """Restore persisted state; the threshold always comes from the current config. Tolerates missing keys."""
        ks = cls(max_daily_loss_pct)
        day = d.get("day")
        ks.day = None if day is None else str(day)
        ks.day_start_equity = _opt_float(d.get("day_start_equity"))
        ks.last_equity = _opt_float(d.get("last_equity"))
        ks.tripped = bool(d.get("tripped", False))
        ks.tripped_at = _opt_int(d.get("tripped_at"))
        reason = d.get("reason")
        ks.reason = None if reason is None else str(reason)
        return ks

    def __repr__(self) -> str:
        return (
            f"DailyLossKillSwitch(pct={self.max_daily_loss_pct}, day={self.day}, "
            f"day_start_equity={self.day_start_equity}, last_equity={self.last_equity}, tripped={self.tripped})"
        )

    @staticmethod
    def _check_equity(equity: float) -> float:
        eq = float(equity)
        if not math.isfinite(eq):
            raise ValueError(f"equity must be a finite number, got {equity!r}")
        return eq


# ---------------------------------------------------------------------------------------------
# Cooldown after a stop-out / liquidation
# ---------------------------------------------------------------------------------------------


class Cooldown:
    """Blocks entry DECISIONS for ``bars`` bars starting with the bar in which the stop-out happened.

    bars=3, stop-out in bar T: decisions at the close of T, T+i, T+2i are blocked; T+3i may open (fill at T+4i).
    bars=0 blocks nothing. Exits are never blocked.
    """

    def __init__(self, bars: int, interval_ms: int) -> None:
        if isinstance(bars, bool) or int(bars) != bars or int(bars) < 0:
            raise ValueError(f"cooldown bars must be an integer >= 0, got {bars!r}")
        if isinstance(interval_ms, bool) or int(interval_ms) != interval_ms or int(interval_ms) <= 0:
            raise ValueError(f"interval_ms must be a positive integer, got {interval_ms!r}")
        self.bars: int = int(bars)
        self.interval_ms: int = int(interval_ms)
        self.until_ms: int | None = None

    def trigger(self, stop_bar_open_time_ms: int) -> None:
        """Start the cooldown: ``until_ms = stop_bar_open_time + bars * interval_ms``."""
        until = int(stop_bar_open_time_ms) + self.bars * self.interval_ms
        # Never shorten a cooldown that is already running further into the future (conservative).
        if self.until_ms is None or until > self.until_ms:
            self.until_ms = until

    def active(self, decision_bar_open_time_ms: int) -> bool:
        """True while ``decision_bar_open_time < until_ms`` (strict)."""
        return self.until_ms is not None and int(decision_bar_open_time_ms) < self.until_ms

    def to_dict(self) -> dict[str, Any]:
        return {"until_ms": self.until_ms, "bars": self.bars, "interval_ms": self.interval_ms}

    @classmethod
    def from_dict(cls, d: Mapping[str, Any], bars: int, interval_ms: int) -> Cooldown:
        """Restore persisted state; ``bars``/``interval_ms`` come from the current config."""
        cd = cls(bars, interval_ms)
        cd.until_ms = _opt_int(d.get("until_ms"))
        return cd

    def __repr__(self) -> str:
        return f"Cooldown(bars={self.bars}, interval_ms={self.interval_ms}, until_ms={self.until_ms})"
