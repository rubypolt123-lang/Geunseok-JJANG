from __future__ import annotations

import anthropic
import httpx2
import pytest

from chart_analyzer import cli
from chart_analyzer.analyzer import AnalysisError
from chart_analyzer.imaging import ClipboardSource

from .conftest import make_chart_image, sample_result


def parse(*argv: str):
    return cli.build_parser().parse_args(list(argv))


def recording_analyze(calls: list):
    def analyze(image, **kwargs):
        calls.append((image, kwargs))
        return sample_result()

    return analyze


def test_parser_defaults():
    args = parse("watch")
    assert args.model == "claude-opus-5-5"
    assert args.effort == "high"
    assert args.folder == [] and args.no_clipboard is False
    assert str(args.out) == "chart_reports"


def test_file_command_writes_report(tmp_path, chart_image):
    path = tmp_path / "chart.png"
    chart_image.save(path)
    args = parse("file", str(path), "--out", str(tmp_path / "out"), "--no-open", "--note", "메모", "--effort", "max")
    calls: list = []

    assert cli.cmd_file(args, analyze=recording_analyze(calls)) == 0
    assert calls[0][1] == {"note": "메모", "model": "claude-opus-5-5", "effort": "max"}
    assert len(list((tmp_path / "out").glob("*.html"))) == 2  # 보고서 + index


def test_file_command_reports_unreadable_file(tmp_path, capsys):
    bad = tmp_path / "bad.png"
    bad.write_text("not an image", encoding="utf-8")
    args = parse("file", str(bad), "--out", str(tmp_path / "out"), "--no-open")
    assert cli.cmd_file(args, analyze=recording_analyze([])) == 1
    assert "이미지를 열 수 없습니다" in capsys.readouterr().out


def test_watch_analyzes_only_new_clipboard_captures(tmp_path):
    existing, new = make_chart_image(300, 200), make_chart_image(500, 300)
    sequence = iter([existing, existing, new, new, new])
    args = parse("watch", "--out", str(tmp_path), "--no-open")
    calls: list = []

    code = cli.cmd_watch(
        args,
        analyze=recording_analyze(calls),
        sources=[ClipboardSource(grab=lambda: next(sequence))],
        sleep=lambda _: None,
        max_polls=4,
    )

    assert code == 0
    assert len(calls) == 1
    assert (calls[0][0].width, calls[0][0].height) == (500, 300)


def test_watch_without_sources_exits(tmp_path):
    args = parse("watch", "--no-clipboard", "--out", str(tmp_path))
    assert cli.cmd_watch(args, sources=[]) == 2


@pytest.mark.parametrize(
    ("exc", "expected"),
    [
        (AnalysisError("모델이 거절했습니다"), "거절"),
        (
            anthropic.AuthenticationError(
                "bad key",
                response=httpx2.Response(401, request=httpx2.Request("POST", "https://api.anthropic.com")),
                body=None,
            ),
            "ANTHROPIC_API_KEY",
        ),
        (anthropic.APIConnectionError(request=httpx2.Request("POST", "https://api.anthropic.com")), "인터넷"),
        (TypeError('"Could not resolve authentication method. Expected one of api_key"'), "API 키가 없습니다"),
    ],
)
def test_api_errors_do_not_stop_processing(tmp_path, capsys, chart_image, exc, expected):
    def failing(image, **kwargs):
        raise exc

    args = parse("clip", "--out", str(tmp_path), "--no-open")
    assert cli.process_capture(cli.Capture(chart_image, "클립보드"), args, analyze=failing) is None
    assert expected in capsys.readouterr().out


def test_unexpected_errors_are_not_swallowed(tmp_path, chart_image):
    def broken(image, **kwargs):
        raise ZeroDivisionError

    args = parse("clip", "--out", str(tmp_path), "--no-open")
    with pytest.raises(ZeroDivisionError):
        cli.process_capture(cli.Capture(chart_image, "클립보드"), args, analyze=broken)
