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
from bot.config import ENV_TESTNET_KEY, ENV_TESTNET_SECRET, AppConfig, load_config, load_credentials
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


def test_trade_args_never_live() -> None:
    assert launcher.trade_args(Mode.PAPER) == ["trade", "--mode", "paper", "--stop-on-stdin"]
    assert launcher.trade_args(Mode.TESTNET) == ["trade", "--mode", "testnet", "--stop-on-stdin"]
    with pytest.raises(ConfigError):
        launcher.trade_args(Mode.LIVE)
    # the CLI accepts exactly what the launcher sends
    args = cli.build_parser().parse_args(launcher.trade_args(Mode.TESTNET))
    assert (args.command, args.mode, args.stop_on_stdin) == ("trade", "testnet", True)


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
    path = launcher.save_testnet_keys(project, f"  {KEY}\n", SECRET)
    text = path.read_text(encoding="utf-8")
    assert f"BINANCE_TESTNET_API_KEY={KEY}\n" in text
    assert f"BINANCE_TESTNET_API_SECRET={SECRET}\n" in text
    assert "BINANCE_API_KEY=\n" in text  # the rest of .env.example is kept (live keys stay empty)
    assert text.count(ENV_TESTNET_KEY + "=") == 1

    cfg = dataclasses.replace(load_config(project / "config.yaml"), mode=Mode.TESTNET)
    creds = load_credentials(cfg, environ={})
    assert creds is not None and (creds.api_key, creds.api_secret) == (KEY, SECRET)
    assert launcher.has_testnet_keys(project / ".env", environ={})

    # saving again replaces the pair instead of appending a second one
    launcher.save_testnet_keys(project, "C" * 64, SECRET)
    text = path.read_text(encoding="utf-8")
    assert text.count(ENV_TESTNET_KEY + "=") == 1 and "C" * 64 in text


@pytest.mark.parametrize("bad", ["", "short", f'"{KEY}"', f"{KEY[:30]} {KEY[30:]}", "가" * 64, f"{KEY}#x"])
def test_save_testnet_keys_rejects_malformed_values(project: Path, bad: str) -> None:
    with pytest.raises(ConfigError):
        launcher.save_testnet_keys(project, bad, SECRET)
    with pytest.raises(ConfigError):
        launcher.save_testnet_keys(project, KEY, bad)
    assert not (project / ".env").exists()


def test_has_testnet_keys_reads_env_file_and_environment(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    assert not launcher.has_testnet_keys(env_file, environ={})
    env_file.write_text(f"{ENV_TESTNET_KEY}={KEY}\n{ENV_TESTNET_SECRET}=\n", encoding="utf-8")
    assert not launcher.has_testnet_keys(env_file, environ={})  # the secret is empty
    assert launcher.has_testnet_keys(env_file, environ={ENV_TESTNET_SECRET: SECRET})


def test_config_summary(app_config: AppConfig) -> None:
    text = launcher.config_summary(app_config)
    assert "BTCUSDT · 1h 봉 · 전략 ma_cross (fast_period=20, slow_period=50, ma_type=EMA, allow_short=True)" in text
    assert "레버리지 3배 · 1회 위험 1% · 손절 ATR×2 · 익절 2R · 일일 손실 한도 5%" in text


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
    assert "BTCUSDT" in app.summary_var.get()
    assert "없음" in app.keys_var.get()
    assert app.start_var.get() == "2024-01-01"
    assert str(app.backtest_btn.cget("state")) == "normal"
    assert str(app.trade_stop_btn.cget("state")) == "disabled"

    # the key dialog writes .env and the window picks it up
    dialog = launcher.KeyDialog(app)
    dialog.key_var.set(KEY)
    dialog.secret_var.set(SECRET)
    dialog.save()
    assert launcher.has_testnet_keys(project / ".env", environ={})
    assert "입력됨" in app.keys_var.get()

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
