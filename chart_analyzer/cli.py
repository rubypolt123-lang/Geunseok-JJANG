"""명령줄 진입점.

``python -m chart_analyzer`` 만 실행하면 창(GUI)이 뜨고, ``watch`` / ``clip`` / ``file`` 은 콘솔용입니다.
"""

from __future__ import annotations

import argparse
import sys
import time
import webbrowser
from collections.abc import Callable, Sequence
from pathlib import Path

from . import analyzer, settings
from .analyzer import AnalysisResult, describe_error
from .imaging import (
    Capture,
    ClipboardSource,
    FolderSource,
    PreparedImage,
    grab_clipboard_image,
    load_image,
    prepare_image,
)
from .report import BIAS_LABEL, SavedReport, save_report

AnalyzeFn = Callable[..., AnalysisResult]


def process_capture(
    capture: Capture,
    args: argparse.Namespace,
    analyze: AnalyzeFn = analyzer.analyze_chart,
) -> SavedReport | None:
    """캡처 한 장을 분석해 보고서로 저장합니다. 실패하면 이유를 출력하고 None."""
    image: PreparedImage = prepare_image(capture.image)
    print(f"\n▶ 분석 중... ({capture.source}, {image.width}×{image.height}px, 보통 30초~2분)", flush=True)
    started = time.monotonic()
    try:
        result = analyze(image, note=args.note, model=args.model, effort=args.effort)
    except Exception as exc:  # noqa: BLE001 - 사용자에게 보여줄 메시지로 바꿉니다
        print(f"✖ 분석 실패: {describe_error(exc)}", flush=True)
        return None

    try:
        saved = save_report(result, image, args.out, source=capture.source)
    except OSError as exc:
        print(f"✖ 보고서를 저장하지 못했습니다: {exc}", flush=True)
        return None
    a = result.analysis
    elapsed = time.monotonic() - started
    print(f"✔ {a.title}  [{BIAS_LABEL[a.overall_bias]}]  ({elapsed:.0f}초)")
    for line in a.summary:
        print(f"   · {line}")
    print(f"   보고서: {saved.html}", flush=True)
    if not args.no_open:
        webbrowser.open(saved.html.resolve().as_uri())
    return saved


def build_sources(args: argparse.Namespace) -> list[ClipboardSource | FolderSource]:
    sources: list[ClipboardSource | FolderSource] = []
    if not args.no_clipboard:
        sources.append(ClipboardSource())
    sources.extend(FolderSource(folder) for folder in args.folder)
    return sources


def cmd_watch(
    args: argparse.Namespace,
    analyze: AnalyzeFn = analyzer.analyze_chart,
    sources: list[ClipboardSource | FolderSource] | None = None,
    sleep: Callable[[float], None] = time.sleep,
    max_polls: int | None = None,
) -> int:
    if sources is None:
        sources = build_sources(args)
    if not sources:
        print("감시할 대상이 없습니다. --no-clipboard 를 쓸 때는 --folder 를 지정하세요.")
        return 2

    for source in sources:
        source.prime()
    print("차트 캡처를 기다리는 중입니다. (끝내려면 Ctrl+C)")
    if not args.no_clipboard:
        print(" - 클립보드: Win+Shift+S 로 차트 영역을 캡처하면 바로 분석합니다.")
    for folder in args.folder:
        print(f" - 폴더: {folder} 에 새 이미지가 저장되면 분석합니다.")
    print(f" - 보고서 저장 위치: {args.out.resolve()}", flush=True)

    polls = 0
    try:
        while max_polls is None or polls < max_polls:
            polls += 1
            for source in sources:
                for capture in source.poll():
                    process_capture(capture, args, analyze)
            sleep(args.interval)
    except KeyboardInterrupt:
        print("\n감시를 끝냅니다.")
    return 0


def cmd_clip(args: argparse.Namespace, analyze: AnalyzeFn = analyzer.analyze_chart) -> int:
    image = grab_clipboard_image()
    if image is None:
        print("클립보드에 이미지가 없습니다. Win+Shift+S 로 차트를 캡처한 뒤 다시 실행하세요.")
        return 1
    return 0 if process_capture(Capture(image, "클립보드"), args, analyze) else 1


def cmd_file(args: argparse.Namespace, analyze: AnalyzeFn = analyzer.analyze_chart) -> int:
    failures = 0
    for path in args.paths:
        try:
            image = load_image(path)
        except OSError as exc:
            print(f"✖ 이미지를 열 수 없습니다: {path} ({exc})")
            failures += 1
            continue
        if process_capture(Capture(image, str(path)), args, analyze) is None:
            failures += 1
    return 1 if failures else 0


def cmd_shortcut() -> int:
    from .shortcut import SHORTCUT_NAME, create_desktop_shortcut

    try:
        create_desktop_shortcut()
    except (RuntimeError, OSError) as exc:
        print(f"바탕화면 바로가기를 만들지 못했습니다: {exc}")
        return 1
    print(f"바탕화면에 '{SHORTCUT_NAME}' 바로가기를 만들었습니다.")
    return 0


def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--note", help="분석에 참고할 메모. 예: \"BTC 4시간봉, 롱 진입 고민 중\"")
    common.add_argument("--out", type=Path, default=settings.REPORTS_DIR, help="보고서 저장 폴더 (기본: chart_reports)")
    common.add_argument("--model", choices=analyzer.MODEL_CHOICES, default=analyzer.DEFAULT_MODEL, help="분석 모델")
    common.add_argument(
        "--effort", choices=analyzer.EFFORT_CHOICES, default=analyzer.DEFAULT_EFFORT,
        help="분석 깊이 (높을수록 꼼꼼하지만 느리고 비쌉니다, 기본: high)",
    )
    common.add_argument("--no-open", action="store_true", help="보고서를 브라우저로 자동으로 열지 않습니다")

    parser = argparse.ArgumentParser(
        prog="python -m chart_analyzer",
        description="차트 캡처 이미지를 Claude 로 분석해 보고서(HTML/Markdown)로 저장합니다.",
    )
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("gui", help="차트 분석기 창을 엽니다 (아무 명령 없이 실행해도 같음)")
    sub.add_parser("shortcut", help="바탕화면에 '차트 분석기' 바로가기를 만듭니다 (윈도우)")

    watch = sub.add_parser("watch", parents=[common], help="캡처를 감시하다가 새 차트가 들어오면 자동 분석")
    watch.add_argument("--folder", type=Path, action="append", default=[], help="새 이미지 파일을 감시할 폴더 (여러 번 지정 가능)")
    watch.add_argument("--no-clipboard", action="store_true", help="클립보드는 감시하지 않습니다")
    watch.add_argument("--interval", type=float, default=1.0, help="확인 주기(초, 기본 1)")

    sub.add_parser("clip", parents=[common], help="지금 클립보드에 있는 이미지를 한 번 분석")

    file_cmd = sub.add_parser("file", parents=[common], help="이미지 파일을 분석")
    file_cmd.add_argument("paths", type=Path, nargs="+", help="분석할 이미지 파일")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(encoding="utf-8", errors="replace")
    settings.load_env()
    args = build_parser().parse_args(argv)
    if args.command in (None, "gui"):
        from .app import run

        return run()
    if args.command == "shortcut":
        return cmd_shortcut()
    if args.command == "watch":
        return cmd_watch(args)
    if args.command == "clip":
        return cmd_clip(args)
    return cmd_file(args)
