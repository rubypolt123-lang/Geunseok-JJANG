"""Read-only local web dashboard (SPEC §12.3).

- Only GET routes (plus the ``/static`` mount). No trading controls, no POST/PUT/PATCH/DELETE.
- Every request gets a query-only ``Storage`` (``read_only=True``: no DDL, no journal_mode change). If the database
  file does not exist the dependency yields ``None`` and every route answers its empty payload; the dashboard never
  creates the database or its folder.
- ``TrustedHostMiddleware`` rejects foreign ``Host`` headers (DNS-rebinding guard); ``run_dashboard`` binds to
  loopback addresses only.
- Chart points carry ``time`` in UTC **seconds** (lightweight-charts); table rows keep the stored ms fields.
"""

from __future__ import annotations

import logging
import math
import sqlite3
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from pathlib import Path
from typing import Annotated, Any, Final

import uvicorn
from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.datastructures import MutableHeaders
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from bot import __version__
from bot.config import LOOPBACK_HOSTS, AppConfig
from bot.errors import ConfigError, DataError
from bot.models import METRIC_LABELS_KO, PERCENT_METRICS, Direction, ExitReason, to_jsonable
from bot.storage import Storage
from bot.timeutil import INTERVAL_MS, now_ms

logger = logging.getLogger(__name__)

CDN_LIGHTWEIGHT_CHARTS: Final = (
    "https://cdn.jsdelivr.net/npm/lightweight-charts@4.2.0/dist/lightweight-charts.standalone.production.js"
)

# Korean exit-reason labels for chart markers (app.js carries the same map for its tables).
EXIT_REASON_KO: Final[dict[str, str]] = {
    ExitReason.SIGNAL.value: "신호 청산",
    ExitReason.FLIP.value: "포지션 전환",
    ExitReason.STOP_LOSS.value: "손절",
    ExitReason.TAKE_PROFIT.value: "익절",
    ExitReason.LIQUIDATION.value: "강제청산",
    ExitReason.KILL_SWITCH.value: "킬스위치",
    ExitReason.END_OF_DATA.value: "백테스트 종료",
    ExitReason.PROTECTION_FAILED.value: "보호주문 실패",
    ExitReason.MANUAL.value: "수동 청산",
    ExitReason.UNKNOWN.value: "알 수 없음",
}

STATIC_DIR: Final[Path] = Path(__file__).resolve().parent / "static"
INDEX_HTML: Final[Path] = STATIC_DIR / "index.html"

# DNS-rebinding guard: only loopback names (and Starlette's TestClient host) may address the dashboard.
ALLOWED_HOSTS: Final[tuple[str, ...]] = ("127.0.0.1", "localhost", "[::1]", "::1", "testserver")

MAX_CHART_POINTS: Final = 5000  # backtest equity is downsampled above this many points
MIN_STALE_SEC: Final = 90.0  # stale = heartbeat age > max(3 * heartbeat_sec, 90)

# Chart marker styling (lightweight-charts v4 setMarkers)
_COLOR_LONG: Final = "#26a69a"
_COLOR_SHORT: Final = "#ef5350"
_COLOR_EXIT: Final = "#607d8b"
_MARKER_TRADE_SCAN_LIMIT: Final = 5000  # newest trades scanned for markers inside the candle window

_BACKTEST_RUN_KEYS: Final = (
    "run_id",
    "created_at",
    "symbol",
    "interval",
    "strategy",
    "params",
    "start_time",
    "end_time",
    "initial_balance",
    "metrics",
)


# ---------------------------------------------------------------------------------------------
# Response headers (pure ASGI middleware: no BaseHTTPMiddleware)
# ---------------------------------------------------------------------------------------------


class _CacheControlMiddleware:
    """``/api/*`` -> ``Cache-Control: no-store``; the page and static assets -> ``no-cache`` (revalidate)."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        path: str = scope.get("path", "")
        if path == "/api" or path.startswith("/api/"):
            value = "no-store"
        elif path == "/" or path.startswith("/static/"):
            value = "no-cache"
        else:
            await self.app(scope, receive, send)
            return

        async def send_with_header(message: Message) -> None:
            if message["type"] == "http.response.start":
                MutableHeaders(scope=message)["Cache-Control"] = value
            await send(message)

        await self.app(scope, receive, send_with_header)


# ---------------------------------------------------------------------------------------------
# Dependencies
# ---------------------------------------------------------------------------------------------


def _app_config(request: Request) -> AppConfig:
    return request.app.state.cfg


def _now_ms(request: Request) -> int:
    clock: Callable[[], float] = request.app.state.clock
    return now_ms(clock)


def _read_only_storage(request: Request) -> Iterator[Storage | None]:
    """One query-only connection per request; ``None`` when the database is missing or unreadable.

    Never creates the file or its folder (``Storage(read_only=True)`` refuses a missing file and runs no DDL).
    """
    cfg = _app_config(request)
    db_path = cfg.db_path
    if not db_path.is_file():
        yield None
        return
    try:
        st = Storage(db_path, read_only=True)
    except (DataError, sqlite3.Error) as exc:
        # SPEC-GAP: §12.3 only defines the "file missing" case. A database that exists but cannot be opened as a
        # bot database (being created right now by the trader, wrong schema version, not SQLite) is treated the
        # same way (empty payloads) instead of failing every request; the reason is logged.
        logger.warning("dashboard cannot open %s read-only: %s", db_path, exc)
        yield None
        return
    with st:
        yield st


ConfigDep = Annotated[AppConfig, Depends(_app_config)]
NowDep = Annotated[int, Depends(_now_ms)]
StorageDep = Annotated[Storage | None, Depends(_read_only_storage)]


# ---------------------------------------------------------------------------------------------
# Pure helpers (unit-tested)
# ---------------------------------------------------------------------------------------------


def _json(payload: Mapping[str, Any], status_code: int = 200) -> JSONResponse:
    """NaN/inf -> null, enums -> values, numpy scalars -> native (``to_jsonable``) before rendering."""
    return JSONResponse(to_jsonable(payload), status_code=status_code)


def _clean(value: str | None) -> str | None:
    """Query strings like ``?source=`` arrive as ""; treat blank as "not given"."""
    if value is None:
        return None
    text = value.strip()
    return text or None


def _to_seconds(ms: Any) -> int:
    return int(ms) // 1000


def heartbeat_stale_after_sec(heartbeat_sec: int) -> float:
    """Heartbeat age above which the trader counts as not responding."""
    return max(3.0 * float(heartbeat_sec), MIN_STALE_SEC)


def status_payload(status: Mapping[str, Any], *, now: int, heartbeat_sec: int) -> dict[str, Any]:
    """``get_status()`` keys + heartbeat_age_sec (LOCAL clock), stale, position, protective_orders."""
    updated_at = status.get("updated_at")
    age = (int(now) - int(updated_at)) / 1000.0 if updated_at is not None else math.inf
    account = status.get("account")
    position: Any = None
    protective: Any = []
    if isinstance(account, Mapping):
        position = account.get("position") or None
        protective = account.get("protective_orders") or []
    out = dict(status)
    out["heartbeat_age_sec"] = float(age) if math.isfinite(age) else None
    out["stale"] = bool(not math.isfinite(age) or age > heartbeat_stale_after_sec(heartbeat_sec))
    out["position"] = position
    out["protective_orders"] = list(protective)
    return out


def downsample_points(points: Sequence[Any], max_points: int = MAX_CHART_POINTS) -> list[Any]:
    """Keep every ``ceil(n / max_points)``-th point plus the last one (n <= max_points: unchanged)."""
    n = len(points)
    if n <= max_points:
        return list(points)
    step = math.ceil(n / max_points)
    out = list(points[::step])
    if (n - 1) % step != 0:
        out.append(points[-1])
    return out


def _interval_ms_or_none(interval: str) -> int | None:
    return INTERVAL_MS.get(str(interval))


def _floor_seconds(ts_ms: int, interval_ms: int | None) -> int:
    ts = int(ts_ms)
    if interval_ms:
        ts -= ts % interval_ms
    return ts // 1000


def build_markers(
    trades: Sequence[Mapping[str, Any]],
    *,
    symbol: str,
    interval: str,
    range_start_ms: int,
    range_end_ms: int,
) -> list[dict[str, Any]]:
    """Entry/exit markers of ``trades`` whose times fall inside [range_start_ms, range_end_ms], sorted by time.

    Entry: long -> belowBar arrowUp green "롱 진입"; short -> aboveBar arrowDown red "숏 진입".
    Exit: circle gray, aboveBar for a long / belowBar for a short, text = EXIT_REASON_KO[reason].
    Marker times are floored to the chart interval (bar open, UTC seconds) so they sit on a candle.
    """
    interval_ms = _interval_ms_or_none(interval)
    keyed: list[tuple[tuple[int, int, int], dict[str, Any]]] = []
    for t in trades:
        if str(t.get("symbol")) != symbol:
            continue
        direction = str(t.get("direction") or "")
        is_long = direction == Direction.LONG.value
        if direction not in (Direction.LONG.value, Direction.SHORT.value):
            continue
        entry_time = t.get("entry_time")
        exit_time = t.get("exit_time")
        entry_key = int(entry_time) if entry_time is not None else 0
        if entry_time is not None and range_start_ms <= int(entry_time) <= range_end_ms:
            marker = {
                "time": _floor_seconds(entry_time, interval_ms),
                "position": "belowBar" if is_long else "aboveBar",
                "shape": "arrowUp" if is_long else "arrowDown",
                "color": _COLOR_LONG if is_long else _COLOR_SHORT,
                "text": "롱 진입" if is_long else "숏 진입",
            }
            keyed.append(((marker["time"], entry_key, 0), marker))
        if exit_time is not None and range_start_ms <= int(exit_time) <= range_end_ms:
            reason = str(t.get("exit_reason") or ExitReason.UNKNOWN.value)
            marker = {
                "time": _floor_seconds(exit_time, interval_ms),
                "position": "aboveBar" if is_long else "belowBar",
                "shape": "circle",
                "color": _COLOR_EXIT,
                "text": EXIT_REASON_KO.get(reason, reason),
            }
            keyed.append(((marker["time"], entry_key, 1), marker))
    # ascending time (lightweight-charts requirement); same bar: older trade first, entry before exit
    keyed.sort(key=lambda item: item[0])
    return [m for _, m in keyed]


def _current_mode(status: Mapping[str, Any] | None, cfg: AppConfig) -> str:
    if status and status.get("mode"):
        return str(status["mode"])
    return str(cfg.mode.value)


# ---------------------------------------------------------------------------------------------
# Route handlers (GET only; registered on the app in create_app via _GET_ROUTES)
# ---------------------------------------------------------------------------------------------


def index() -> FileResponse:
    return FileResponse(INDEX_HTML, media_type="text/html; charset=utf-8")


def api_health(now: NowDep) -> JSONResponse:
    return _json({"ok": True, "time": now})


def api_meta(cfg: ConfigDep) -> JSONResponse:
    return _json(
        {
            "mode": cfg.mode.value,
            "symbol": cfg.symbol,
            "interval": cfg.interval,
            "refresh_sec": cfg.dashboard.refresh_sec,
            "heartbeat_sec": cfg.execution.heartbeat_sec,
            "version": __version__,
            "read_only": True,
            "metric_labels": dict(METRIC_LABELS_KO),
            "percent_metrics": sorted(PERCENT_METRICS),
        }
    )


def api_status(cfg: ConfigDep, now: NowDep, st: StorageDep) -> JSONResponse:
    status = st.get_status() if st is not None else None
    if status is None:
        return _json({"status": None})
    return _json({"status": status_payload(status, now=now, heartbeat_sec=cfg.execution.heartbeat_sec)})


def api_trades(
    cfg: ConfigDep,
    st: StorageDep,
    source: Annotated[str | None, Query(max_length=32)] = None,
    run_id: Annotated[str | None, Query(max_length=128)] = None,
    limit: Annotated[int, Query(ge=1, le=2000)] = 200,
) -> JSONResponse:
    if st is None:
        return _json({"trades": []})
    src = _clean(source) or _current_mode(st.get_status(), cfg)
    trades = st.list_trades(source=src, run_id=_clean(run_id), limit=limit)
    return _json({"trades": trades})


def api_equity(
    cfg: ConfigDep,
    st: StorageDep,
    mode: Annotated[str | None, Query(max_length=32)] = None,
    limit: Annotated[int, Query(ge=1, le=50000)] = 5000,
) -> JSONResponse:
    if st is None:
        return _json({"mode": _clean(mode) or cfg.mode.value, "points": []})
    m = _clean(mode) or _current_mode(st.get_status(), cfg)
    points = [
        {"time": _to_seconds(p["time"]), "equity": p["equity"], "wallet": p["wallet"]}
        for p in st.equity_curve(m, limit=limit)
    ]
    return _json({"mode": m, "points": points})


def api_candles(
    cfg: ConfigDep,
    st: StorageDep,
    limit: Annotated[int, Query(ge=1, le=5000)] = 300,
) -> JSONResponse:
    if st is None:
        return _json({"symbol": cfg.symbol, "interval": cfg.interval, "candles": [], "markers": []})
    status = st.get_status()
    symbol = str(status["symbol"]) if status and status.get("symbol") else cfg.symbol
    interval = str(status["interval"]) if status and status.get("interval") else cfg.interval
    mode = _current_mode(status, cfg)
    rows = st.get_candles(symbol, interval, limit=limit)
    candles = [
        {
            "time": _to_seconds(r["open_time"]),
            "open": r["open"],
            "high": r["high"],
            "low": r["low"],
            "close": r["close"],
        }
        for r in rows
    ]
    markers: list[dict[str, Any]] = []
    if rows:
        interval_ms = _interval_ms_or_none(interval) or 0
        range_start = int(rows[0]["open_time"])
        range_end = int(rows[-1]["open_time"]) + max(interval_ms - 1, 0)
        trades = st.list_trades(source=mode, limit=_MARKER_TRADE_SCAN_LIMIT)
        markers = build_markers(
            trades, symbol=symbol, interval=interval, range_start_ms=range_start, range_end_ms=range_end
        )
    return _json({"symbol": symbol, "interval": interval, "candles": candles, "markers": markers})


def api_events(
    st: StorageDep,
    mode: Annotated[str | None, Query(max_length=32)] = None,
    limit: Annotated[int, Query(ge=1, le=1000)] = 50,
) -> JSONResponse:
    if st is None:
        return _json({"events": []})
    return _json({"events": st.recent_events(limit=limit, mode=_clean(mode))})


def api_backtests(
    st: StorageDep,
    limit: Annotated[int, Query(ge=1, le=1000)] = 50,
) -> JSONResponse:
    if st is None:
        return _json({"runs": []})
    runs = [{k: run.get(k) for k in _BACKTEST_RUN_KEYS} for run in st.list_backtests(limit=limit)]
    return _json({"runs": runs})


def api_backtest_detail(
    run_id: str,
    st: StorageDep,
    trade_limit: Annotated[int, Query(ge=1, le=100000)] = 10000,
) -> JSONResponse:
    run = st.get_backtest(run_id) if st is not None else None
    if run is None or st is None:
        raise HTTPException(status_code=404, detail="backtest not found")
    equity = downsample_points(
        [{"time": _to_seconds(p["time"]), "equity": p["equity"]} for p in st.backtest_equity(run_id)]
    )
    trades = st.list_trades(source="backtest", run_id=run_id, limit=trade_limit)
    return _json({"run": run, "equity": equity, "trades": trades})


# (path, handler, include_in_schema). Registered directly on the app (not through an included APIRouter) so that
# ``app.routes`` lists every APIRoute with its methods — the read-only test inspects exactly that.
_GET_ROUTES: Final[tuple[tuple[str, Callable[..., Any], bool], ...]] = (
    ("/", index, False),
    ("/api/health", api_health, True),
    ("/api/meta", api_meta, True),
    ("/api/status", api_status, True),
    ("/api/trades", api_trades, True),
    ("/api/equity", api_equity, True),
    ("/api/candles", api_candles, True),
    ("/api/events", api_events, True),
    ("/api/backtests", api_backtests, True),
    ("/api/backtests/{run_id}", api_backtest_detail, True),
)


# ---------------------------------------------------------------------------------------------
# App factory / server
# ---------------------------------------------------------------------------------------------


def create_app(cfg: AppConfig, *, clock: Callable[[], float] = time.time) -> FastAPI:
    """Build the read-only dashboard app. ``clock`` (LOCAL seconds) is injectable for tests."""
    app = FastAPI(
        title="binance-futures-bot dashboard",
        version=__version__,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.state.cfg = cfg
    app.state.clock = clock
    for path, endpoint, in_schema in _GET_ROUTES:
        app.add_api_route(path, endpoint, methods=["GET"], include_in_schema=in_schema)
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    # add_middleware prepends: the host check (added last) is the outermost layer.
    app.add_middleware(_CacheControlMiddleware)
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=list(ALLOWED_HOSTS))
    return app


def run_dashboard(cfg: AppConfig, host: str, port: int) -> None:
    """Serve the dashboard on a loopback address (called after ``setup_logging(..., log_name="dashboard")``).

    ``log_config=None`` keeps uvicorn from ``dictConfig()``-ing away the redacting handlers of §12.2.
    """
    if host not in LOOPBACK_HOSTS:
        raise ConfigError(
            f"dashboard must bind to localhost only ({', '.join(sorted(LOOPBACK_HOSTS))}); got {host!r} "
            "/ 대시보드는 로컬 주소에서만 실행할 수 있습니다"
        )
    if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
        raise ConfigError(f"dashboard port must be an integer in 1..65535 (got {port!r})")
    app = create_app(cfg)
    shown = f"[{host}]" if ":" in host else host
    logger.info("dashboard (read-only) on http://%s:%d/ using %s", shown, port, cfg.db_path)
    uvicorn.run(app, host=host, port=port, log_config=None, log_level="info", access_log=False)
