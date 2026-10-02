"""투자관리 엑셀 만들기 / 시세 업데이트.

    python -m investment_excel             # 투자관리.xlsx 를 만들거나 시세를 업데이트
    python -m investment_excel --offline   # 인터넷 없이 (예시 데이터로) 파일만 만들기
"""

from __future__ import annotations

import argparse
import os
import shutil
import sys
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path

from . import prices, sample
from .models import Bar
from .workbook import SAMPLE_NOTE, build_workbook, read_workbook

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_PATH = PROJECT_ROOT / "투자관리.xlsx"
BACKUP_DIR_NAME = "투자관리_백업"
KEEP_BACKUPS = 20
REAL_NOTE = "무료 공개 시세 (바이낸스 · 네이버 금융 · 야후 파이낸스). 지연되거나 일부 빠질 수 있습니다."


def _backup(path: Path, now: datetime) -> Path:
    folder = path.parent / BACKUP_DIR_NAME
    folder.mkdir(exist_ok=True)
    target = folder / f"{path.stem}_{now:%Y%m%d-%H%M%S}{path.suffix}"
    shutil.copy2(path, target)
    old = sorted(folder.glob(f"{path.stem}_*{path.suffix}"))
    for extra in old[:-KEEP_BACKUPS]:
        extra.unlink(missing_ok=True)
    return target


def _old_note(path: Path) -> str:
    try:
        from openpyxl import load_workbook

        return str(load_workbook(path, read_only=True)["설정"]["B7"].value or "")
    except Exception:  # noqa: BLE001 - 안내 문구일 뿐이라 못 읽어도 괜찮습니다
        return ""


def run(
    path: Path = DEFAULT_PATH,
    *,
    offline: bool = False,
    get: prices.Fetch = prices.http_get,
    now: datetime | None = None,
) -> int:
    now = now or datetime.now()
    created = not path.exists()
    if created:
        print(f"'{path.name}' 파일이 없어 새로 만듭니다. (예시 종목·거래가 들어 있으니 지우고 쓰세요)")
        inputs = sample.example_inputs(now.date())
        old_bars: dict[str, list[Bar]] = {}
        old_note = ""
    else:
        try:
            inputs, old_bars, warnings = read_workbook(path)
        except PermissionError:
            print("엑셀 파일을 읽지 못했습니다. 엑셀에서 파일을 닫고 다시 실행하세요.")
            return 1
        except Exception as exc:  # noqa: BLE001
            print(f"엑셀 파일을 읽지 못했습니다: {exc}\n'{BACKUP_DIR_NAME}' 폴더의 백업 파일로 바꿔 보세요.")
            return 1
        for warning in warnings:
            print(f"  ! {warning}")
        old_note = _old_note(path)

    bars = dict(old_bars)
    refreshed: set[str] = set()
    if not offline:
        print("시세를 받는 중...")
        for ticker in inputs.tickers:
            if ticker.manual_price is not None and ticker.market == "기타":
                print(f"  - {ticker.code}: 수동 현재가 사용")
                continue
            try:
                fetched = prices.fetch_ticker(ticker, get=get)
            except prices.PriceError as exc:
                kept = "기존 데이터를 그대로 둡니다" if ticker.code in bars else "차트 없이 넘어갑니다"
                print(f"  ✖ {ticker.code}: {exc} → {kept}")
                continue
            bars[ticker.code] = fetched
            refreshed.add(ticker.code)
            print(f"  ✔ {ticker.code} ({ticker.name}): {len(fetched)}일, 마지막 {fetched[-1].day:%Y-%m-%d} 종가 {fetched[-1].close:,.6g}")
        if inputs.auto_fx:
            try:
                inputs.usd_krw = prices.fetch_usd_krw(get)
                print(f"  ✔ USD/KRW 환율: {inputs.usd_krw:,.2f}")
            except prices.PriceError as exc:
                print(f"  ✖ 환율: {exc} → 기존 {inputs.usd_krw:,.2f} 사용")

    using_sample = False
    if created and not refreshed:
        bars = sample.example_bars(now.date())
        using_sample = True
        if not offline:
            print("  시세를 하나도 받지 못해 예시 데이터로 만들었습니다. 인터넷 연결 후 다시 실행하세요.")
    elif old_note == SAMPLE_NOTE and any(code not in refreshed for code in bars):
        using_sample = True

    note = SAMPLE_NOTE if using_sample else REAL_NOTE
    updated_at = now.strftime("%Y-%m-%d %H:%M") if refreshed else ("예시 데이터" if using_sample else "-")
    wb = build_workbook(inputs, bars, updated_at=updated_at, data_note=note)

    backup = None if created else _backup(path, now)
    temp = path.with_name(f"~{path.stem}.tmp{path.suffix}")
    try:
        wb.save(temp)
        os.replace(temp, path)
    except PermissionError:
        temp.unlink(missing_ok=True)
        print("\n✖ 저장하지 못했습니다. 엑셀에서 '투자관리.xlsx' 를 닫고 다시 실행하세요.")
        return 1
    print(f"\n완료: {path}")
    if backup:
        print(f"  (업데이트 전 파일은 {backup.parent.name}\\{backup.name} 에 보관했습니다)")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(prog="python -m investment_excel", description="투자관리 엑셀 만들기 / 시세 업데이트")
    parser.add_argument("path", nargs="?", type=Path, default=DEFAULT_PATH, help="엑셀 파일 경로 (기본: 프로젝트 폴더의 투자관리.xlsx)")
    parser.add_argument("--offline", action="store_true", help="인터넷 없이 만들기 (새 파일이면 예시 데이터)")
    args = parser.parse_args(argv)
    return run(args.path, offline=args.offline)
