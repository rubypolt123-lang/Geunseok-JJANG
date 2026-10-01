"""Beginner launcher (bot/launcher.py): command building, .env handling, child processes, window smoke test."""

from __future__ import annotations

import dataclasses
import queue
import shutil
import sys
import threading
import time
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from bot import cli, launcher
from bot.config import (
    ENV_CONFIRM_LIVE,
    ENV_LIVE_KEY,
    ENV_LIVE_SECRET,
    ENV_TESTNET_KEY,
    ENV_TESTNET_SECRET,
    AppConfig,
    load_config,
    load_credentials,
)
from bot.errors import ConfigError
from bot.models import Mode
from tests.conftest import EXAMPLE_CONFIG, REPO_ROOT

KEY = "A" * 64
SECRET = "b1" * 32


@pytest.fixture
def project(tmp_path: Path) -> Path:
    shutil.copyfile(EXAMPLE_CONFIG, tmp_path / "config.yaml")
    shutil.copyfile(REPO_ROOT / ".env.example", tmp_path / ".env.example")
    return tmp_path


def test_trade_args() -> None:
    assert launcher.trade_args(Mode.PAPER) == ["trade", "--mode", "paper", "--stop-on-stdin"]
    assert launcher.trade_args(Mode.TESTNET) == ["trade", "--mode", "testnet", "--stop-on-stdin"]
    # live is never named on the command line (the CLI refuses it): it comes from config.yaml
    assert launcher.trade_args(Mode.LIVE) == ["trade", "--stop-on-stdin"]
    # the CLI accepts exactly what the launcher sends
    parser = cli.build_parser()
    args = parser.parse_args(launcher.trade_args(Mode.TESTNET))
    assert (args.command, args.mode, args.stop_on_stdin) == ("trade", "testnet", True)
    args = parser.parse_args(launcher.trade_args(Mode.LIVE))
    assert (args.command, args.mode, args.stop_on_stdin) == ("trade", None, True)
    args = parser.parse_args(launcher.compare_args("2024-01-01"))
    assert (args.command, args.start, args.intervals) == ("compare", "2024-01-01", "1h,4h")


def test_child_env_carries_live_confirmation_only_when_asked() -> None:
    base = {"PATH": "/bin", ENV_CONFIRM_LIVE: "YES"}  # even if the user set it globally
    env = launcher.child_env(base=base)
    assert ENV_CONFIRM_LIVE not in env
    assert env["PATH"] == "/bin" and env["PYTHONUTF8"] == "1"
    assert launcher.child_env({ENV_CONFIRM_LIVE: "YES"}, base={"PATH": "/bin"})[ENV_CONFIRM_LIVE] == "YES"


def test_losing_streak_pct() -> None:
    assert launcher.losing_streak_pct(3.0) == pytest.approx(26.26, abs=0.01)
    assert launcher.losing_streak_pct(1.0) == pytest.approx(9.56, abs=0.01)
    assert launcher.losing_streak_pct(2.0, losses=1) == pytest.approx(2.0)


def test_stop_command_matches_cli() -> None:
    assert launcher.STOP_COMMAND == cli.STDIN_STOP_COMMAND


def test_backtest_args_validates_date() -> None:
    assert launcher.backtest_args(" 2024-01-01 ") == ["backtest", "--start", "2024-01-01"]
    for bad in ("", "2024/01/01", "어제"):
        with pytest.raises(ConfigError, match="2024-01-01"):
            launcher.backtest_args(bad)


def test_bot_command_prefers_console_python(tmp_path: Path) -> None:
    pythonw = tmp_path / "pythonw.exe"
    pythonw.write_text("")
    config = tmp_path / "config.yaml"
    # no python.exe next to it: keep the given interpreter
    assert launcher.bot_command(config, "strategies", python=str(pythonw))[0] == str(pythonw)
    (tmp_path / "python.exe").write_text("")
    assert launcher.bot_command(config, "strategies", python=str(pythonw)) == [
        str(tmp_path / "python.exe"),
        "-m",
        "bot",
        "-c",
        str(config),
        "strategies",
    ]


def test_dashboard_url_and_exit_messages() -> None:
    assert launcher.dashboard_url("127.0.0.1", 8000) == "http://127.0.0.1:8000/"
    assert launcher.dashboard_url("::1", 8080) == "http://[::1]:8080/"
    assert "완료" in launcher.exit_message("backtest", 0)
    assert "비교" in launcher.exit_message("compare", 0)
    assert "주문은 보내지 않았습니다" in launcher.exit_message("trade", 130)
    assert "trade.log" in launcher.exit_message("trade", 1)
    assert "설정 오류" in launcher.exit_message("trade", 2)
    assert "-9" in launcher.exit_message("trade", -9)
    assert "포트" in launcher.exit_message("dashboard", 1)


def test_ensure_config_copies_example(tmp_path: Path) -> None:
    with pytest.raises(ConfigError):
        launcher.ensure_config(tmp_path)
    shutil.copyfile(EXAMPLE_CONFIG, tmp_path / "config.example.yaml")
    path = launcher.ensure_config(tmp_path)
    assert path == tmp_path / "config.yaml"
    assert path.read_bytes() == EXAMPLE_CONFIG.read_bytes()
    path.write_text("mode: paper\n", encoding="utf-8")  # an existing config is never overwritten
    assert launcher.ensure_config(tmp_path).read_text(encoding="utf-8") == "mode: paper\n"


def test_set_env_values_replaces_in_place_and_appends() -> None:
    text = "# keys\nBINANCE_TESTNET_API_KEY=\nexport BINANCE_TESTNET_API_SECRET = old\nOTHER=1\nBINANCE_TESTNET_API_KEY=dup\n"
    out = launcher.set_env_values(text, {ENV_TESTNET_KEY: "k", ENV_TESTNET_SECRET: "s", "NEW_ONE": "n"})
    assert out == (
        "# keys\nBINANCE_TESTNET_API_KEY=k\nBINANCE_TESTNET_API_SECRET=s\nOTHER=1\n\nNEW_ONE=n\n"
    )
    assert launcher.set_env_values("", {"A": "1"}) == "A=1\n"


def test_save_testnet_keys_creates_env_from_example(project: Path) -> None:
    path = launcher.save_api_keys(project, Mode.TESTNET, f"  {KEY}\n", SECRET)
    text = path.read_text(encoding="utf-8")
    assert f"BINANCE_TESTNET_API_KEY={KEY}\n" in text
    assert f"BINANCE_TESTNET_API_SECRET={SECRET}\n" in text
    assert "BINANCE_API_KEY=\n" in text  # the rest of .env.example is kept (live keys stay empty)
    assert text.count(ENV_TESTNET_KEY + "=") == 1

    cfg = dataclasses.replace(load_config(project / "config.yaml"), mode=Mode.TESTNET)
    creds = load_credentials(cfg, environ={})
    assert creds is not None and (creds.api_key, creds.api_secret) == (KEY, SECRET)
    assert launcher.has_api_keys(project / ".env", Mode.TESTNET, environ={})
    assert not launcher.has_api_keys(project / ".env", Mode.LIVE, environ={})

    # saving again replaces the pair instead of appending a second one
    launcher.save_api_keys(project, Mode.TESTNET, "C" * 64, SECRET)
    text = path.read_text(encoding="utf-8")
    assert text.count(ENV_TESTNET_KEY + "=") == 1 and "C" * 64 in text


def test_save_live_keys_used_by_live_mode_only(project: Path) -> None:
    launcher.save_api_keys(project, Mode.TESTNET, KEY, SECRET)
    path = launcher.save_api_keys(project, Mode.LIVE, "L" * 64, "s" * 64)
    text = path.read_text(encoding="utf-8")
    assert f"{ENV_LIVE_KEY}={'L' * 64}\n" in text and f"{ENV_LIVE_SECRET}={'s' * 64}\n" in text
    assert f"{ENV_TESTNET_KEY}={KEY}\n" in text  # the testnet pair is untouched
    cfg = load_config(project / "config.yaml")
    live = load_credentials(dataclasses.replace(cfg, mode=Mode.LIVE), environ={})
    testnet = load_credentials(dataclasses.replace(cfg, mode=Mode.TESTNET), environ={})
    assert live is not None and live.api_key == "L" * 64
    assert testnet is not None and testnet.api_key == KEY
    assert launcher.has_api_keys(project / ".env", Mode.LIVE, environ={})


@pytest.mark.parametrize("bad", ["", "short", f'"{KEY}"', f"{KEY[:30]} {KEY[30:]}", "가" * 64, f"{KEY}#x"])
def test_save_testnet_keys_rejects_malformed_values(project: Path, bad: str) -> None:
    with pytest.raises(ConfigError):
        launcher.save_api_keys(project, Mode.TESTNET, bad, SECRET)
    with pytest.raises(ConfigError):
        launcher.save_api_keys(project, Mode.LIVE, KEY, bad)
    assert not (project / ".env").exists()


def test_has_api_keys_reads_env_file_and_environment(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    assert not launcher.has_api_keys(env_file, Mode.TESTNET, environ={})
    env_file.write_text(f"{ENV_TESTNET_KEY}={KEY}\n{ENV_TESTNET_SECRET}=\n", encoding="utf-8")
    assert not launcher.has_api_keys(env_file, Mode.TESTNET, environ={})  # the secret is empty
    assert launcher.has_api_keys(env_file, Mode.TESTNET, environ={ENV_TESTNET_SECRET: SECRET})
    assert not launcher.has_api_keys(env_file, Mode.LIVE, environ={ENV_TESTNET_SECRET: SECRET})


def test_config_summary(app_config: AppConfig) -> None:
    text = launcher.config_summary(app_config)
    assert "BTCUSDT · 1h 봉 · 전략 ma_cross (fast_period=20, slow_period=50, ma_type=EMA, allow_short=True)" in text
    assert "위험도: 기본형 — 레버리지 3배 · 손절 1회에 자산의 1% · 손절 거리 ATR×2 · 익절 2R" in text
    custom = dataclasses.replace(app_config, risk=dataclasses.replace(app_config.risk, leverage=4))
    assert "위험도: 사용자 설정" in launcher.config_summary(custom)


# -- child processes ---------------------------------------------------------------------------------------

ECHO_UNTIL_STOP = (
    "import sys\n"
    "print('ready 한글', flush=True)\n"
    "for line in sys.stdin:\n"
    "    if line.strip() == 'stop':\n"
    "        print('bye', flush=True)\n"
    "        sys.exit(0)\n"
    "print('eof', flush=True)\n"
    "sys.exit(3)\n"
)


def wait_finished(proc: launcher.ManagedProcess, timeout: float = 20.0) -> int:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        code = proc.finished()
        if code is not None:
            return code
        time.sleep(0.02)
    raise AssertionError(f"{proc.name} did not finish")


def drain(sink: queue.Queue[tuple[str, str]]) -> list[str]:
    out = []
    while not sink.empty():
        out.append(sink.get_nowait()[1])
    return out


def test_managed_process_streams_output_and_stops_on_request(tmp_path: Path) -> None:
    sink: queue.Queue[tuple[str, str]] = queue.Queue()
    proc = launcher.ManagedProcess("trade", sink)
    assert not proc.running and not proc.request_stop()
    proc.start([sys.executable, "-c", ECHO_UNTIL_STOP], cwd=tmp_path, with_stdin=True)
    assert proc.running
    with pytest.raises(Exception, match="already running"):
        proc.start([sys.executable, "-c", "pass"], cwd=tmp_path)
    deadline = time.monotonic() + 20
    while sink.empty() and time.monotonic() < deadline:
        time.sleep(0.02)
    assert proc.request_stop() and proc.stop_sent
    assert wait_finished(proc) == 0
    assert proc.finished() is None  # reported once
    assert not proc.running
    assert drain(sink) == ["ready 한글", "bye"]  # UTF-8 end to end, all output delivered before the exit


def test_managed_process_eof_when_stdin_closes(tmp_path: Path) -> None:
    # the launcher closing (or crashing) closes the pipe: the child sees EOF
    sink: queue.Queue[tuple[str, str]] = queue.Queue()
    proc = launcher.ManagedProcess("trade", sink)
    proc.start([sys.executable, "-c", ECHO_UNTIL_STOP], cwd=tmp_path, with_stdin=True)
    assert proc.proc is not None and proc.proc.stdin is not None
    proc.proc.stdin.close()
    assert wait_finished(proc) == 3
    assert drain(sink) == ["ready 한글", "eof"]


def test_managed_process_kill(tmp_path: Path) -> None:
    sink: queue.Queue[tuple[str, str]] = queue.Queue()
    proc = launcher.ManagedProcess("dashboard", sink)
    proc.start([sys.executable, "-c", "import time; time.sleep(60)"], cwd=tmp_path)
    assert not proc.request_stop()  # no stdin pipe: cannot be asked, only killed
    proc.kill()
    assert wait_finished(proc) != 0


# -- dashboard readiness -------------------------------------------------------------------------------------


@pytest.fixture
def health_server() -> Iterator[str]:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            self.send_response(200 if self.path == "/api/health" else 404)
            self.end_headers()
            self.wfile.write(b'{"ok": true}')

        def log_message(self, *args: object) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}/"
    finally:
        server.shutdown()
        server.server_close()


def test_wait_for_dashboard_ready(health_server: str) -> None:
    assert launcher.wait_for_dashboard(health_server, timeout_sec=10)


def test_wait_for_dashboard_gives_up(health_server: str) -> None:
    now = [0.0]

    def sleep(s: float) -> None:
        now[0] += s

    closed = health_server.rsplit(":", 1)[0] + ":9/"  # discard port: nothing listens
    assert not launcher.wait_for_dashboard(closed, timeout_sec=2, sleep=sleep, clock=lambda: now[0])
    assert not launcher.wait_for_dashboard(health_server, alive=lambda: False)  # the server process died


# -- window -----------------------------------------------------------------------------------------------------


@pytest.fixture
def tk_root() -> Iterator[launcher.tk.Tk]:
    try:
        root = launcher.tk.Tk()
    except launcher.tk.TclError as exc:
        pytest.skip(f"no display for Tk: {exc}")
    root.withdraw()
    try:
        yield root
    finally:
        try:
            root.destroy()
        except launcher.tk.TclError:
            pass


def test_window_smoke(project: Path, tk_root: launcher.tk.Tk) -> None:
    app = launcher.LauncherApp(tk_root, project_dir=project)
    tk_root.update()
    assert "BTCUSDT" in app.summary_var.get() and "기본형" in app.summary_var.get()
    assert "테스트넷: 없음" in app.keys_var.get()
    assert app.profile_var.get() == "standard" and app.interval_var.get() == "1h"
    assert app.start_var.get() == "2024-01-01"
    assert str(app.backtest_btn.cget("state")) == "normal"
    assert str(app.trade_stop_btn.cget("state")) == "disabled"

    # the key dialog writes .env and the window picks it up
    dialog = launcher.KeyDialog(app)
    dialog.key_var.set(KEY)
    dialog.secret_var.set(SECRET)
    dialog.save()
    assert launcher.has_api_keys(project / ".env", Mode.TESTNET, environ={})
    assert "테스트넷: 입력됨" in app.keys_var.get() and "실거래: 없음" in app.keys_var.get()

    # a broken config disables the actions instead of crashing
    (project / "config.yaml").write_text("mode: [\n", encoding="utf-8")
    assert app.reload_config() is None
    assert "설정 파일 오류" in app.summary_var.get()
    assert str(app.backtest_btn.cget("state")) == "disabled"
    assert str(app.trade_start_btn.cget("state")) == "disabled"

    app.log("[자동매매] 2026 ERROR something", "error")
    assert "something" in app.log_text.get("1.0", "end")
    app.on_close()  # nothing running: closes at once
    assert app._destroyed


class _FakeLiveDialog:
    """Stands in for LiveConfirmDialog: answers at once, as if the word was (not) typed."""

    answer = True

    def __init__(self, app: launcher.LauncherApp, cfg: AppConfig) -> None:
        self.confirmed = _FakeLiveDialog.answer
        self.win = launcher.tk.Toplevel(app.root)
        self.win.after(10, self.win.destroy)


def test_window_trading_modes_and_presets(
    project: Path, tk_root: launcher.tk.Tk, monkeypatch: pytest.MonkeyPatch
) -> None:
    started: list[dict[str, object]] = []

    def fake_start(self: launcher.ManagedProcess, cmd: list[str], **kwargs: object) -> None:
        started.append({"name": self.name, "cmd": cmd, **kwargs})

    monkeypatch.setattr(launcher.ManagedProcess, "start", fake_start)
    monkeypatch.setattr(launcher, "LiveConfirmDialog", _FakeLiveDialog)
    asked: list[str] = []
    monkeypatch.setattr(launcher.messagebox, "askyesno", lambda title, msg, **kw: asked.append(msg) or True)
    monkeypatch.setattr(launcher.messagebox, "showinfo", lambda *a, **kw: None)
    monkeypatch.setattr(launcher, "KeyDialog", lambda app, mode: started.append({"key_dialog": mode}))
    app = launcher.LauncherApp(tk_root, project_dir=project)
    config = project / "config.yaml"

    # preset + interval are written to config.yaml (aggressive ones ask first)
    app.profile_var.set("very_aggressive")
    app.interval_var.set("4h")
    app.apply_profile()
    assert asked and "26%" in asked[0]
    cfg = load_config(config)
    assert (cfg.interval, cfg.risk.leverage, cfg.risk.risk_per_trade_pct, cfg.risk.take_profit_r) == ("4h", 10, 3.0, None)
    assert "초공격형" in app.summary_var.get()

    # paper: --mode paper, no live confirmation in the child environment
    app.mode_var.set("paper")
    app.start_trading()
    assert started[-1]["cmd"][-4:] == ["trade", "--mode", "paper", "--stop-on-stdin"]
    assert not started[-1]["extra_env"]

    # live without keys: the key dialog opens instead, nothing starts, config untouched
    app.mode_var.set("live")
    n = len(started)
    app.start_trading()
    assert started[n:] == [{"key_dialog": Mode.LIVE}]
    assert load_config(config).mode is Mode.PAPER

    # live, dialog cancelled: nothing starts, config stays paper
    launcher.save_api_keys(project, Mode.LIVE, KEY, SECRET)
    _FakeLiveDialog.answer = False
    try:
        app.start_trading()
    finally:
        _FakeLiveDialog.answer = True
    assert len(started) == n + 1 and load_config(config).mode is Mode.PAPER

    # live, confirmed: mode live in config.yaml + CONFIRM_LIVE_TRADING for this child only
    app.start_trading()
    assert started[-1]["cmd"][-2:] == ["trade", "--stop-on-stdin"]
    assert started[-1]["extra_env"] == {ENV_CONFIRM_LIVE: "YES"}
    assert load_config(config).mode is Mode.LIVE

    # starting paper again puts config.yaml back to paper
    app.mode_var.set("paper")
    app.start_trading()
    assert load_config(config).mode is Mode.PAPER


def test_live_confirm_dialog_needs_the_word(project: Path, tk_root: launcher.tk.Tk) -> None:
    app = launcher.LauncherApp(tk_root, project_dir=project)
    dialog = launcher.LiveConfirmDialog(app, load_config(project / "config.yaml"))
    assert str(dialog.start_btn.cget("state")) == "disabled"
    dialog.word_var.set("실거")
    dialog.confirm()
    assert not dialog.confirmed and dialog.win.winfo_exists()
    dialog.word_var.set(" 실거래 ")
    assert str(dialog.start_btn.cget("state")) == "normal"
    dialog.confirm()
    assert dialog.confirmed
