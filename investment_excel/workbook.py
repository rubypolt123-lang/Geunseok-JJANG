"""투자관리 엑셀 파일 만들기와 읽기.

시트 구성
- 사용법 / 대시보드 / 일봉차트 / 보유현황 : 수식으로 자동 계산 (직접 고칠 곳 없음, 일봉차트의 종목 선택만)
- 거래기록 / 종목 / 설정 : 사용자가 입력하는 곳 (노란 칸)
- 일봉데이터 : 시세 업데이트가 채우는 곳 (직접 입력해도 됨)
- 차트데이터 : 차트용 계산 (숨김)

시세 업데이트는 입력값을 읽어 파일을 새로 만듭니다(openpyxl 은 기존 차트를 보존하지 못하므로).
"""

from __future__ import annotations

import math
from collections.abc import Iterable
from datetime import date, datetime
from pathlib import Path

from openpyxl import Workbook, load_workbook
from openpyxl.chart import BarChart, LineChart, PieChart, Reference, StockChart
from openpyxl.chart.axis import ChartLines
from openpyxl.chart.label import DataLabelList
from openpyxl.chart.legend import Legend
from openpyxl.chart.marker import Marker
from openpyxl.chart.shapes import GraphicalProperties
from openpyxl.chart.updown_bars import UpDownBars
from openpyxl.comments import Comment
from openpyxl.drawing.line import LineProperties
from openpyxl.formatting.rule import CellIsRule
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.datavalidation import DataValidation
from openpyxl.worksheet.worksheet import Worksheet

from .models import (
    CURRENCIES,
    DEFAULT_CURRENCY,
    MARKETS,
    SIDES,
    Bar,
    Inputs,
    Ticker,
    Trade,
)

TICKER_FIRST, TICKER_ROWS = 5, 50
TICKER_LAST = TICKER_FIRST + TICKER_ROWS - 1  # 54
TRADE_FIRST, TRADE_ROWS = 5, 1000
TRADE_LAST = TRADE_FIRST + TRADE_ROWS - 1  # 1004
DATA_LAST = 50000  # 일봉데이터 최대 행
CHART_DAYS = 120
CHART_FIRST = 6  # 차트데이터: 첫 데이터 행
CHART_LAST = CHART_FIRST + CHART_DAYS - 1  # 125

FONT_NAME = "맑은 고딕"
NAVY = "1F3864"
UP_RED = "D32F2F"  # 국내 관례: 상승 빨강
DOWN_BLUE = "1565C0"  # 하락 파랑
INPUT_FILL = PatternFill("solid", fgColor="FFF2CC")
HEADER_FILL = PatternFill("solid", fgColor=NAVY)
TOTAL_FILL = PatternFill("solid", fgColor="E7ECF5")
THIN = Side(style="thin", color="D0D5DD")
BOX = Border(left=THIN, right=THIN, top=THIN, bottom=THIN)

FMT_PRICE = "[>=1000]#,##0;[>=1]#,##0.00;0.00######"
FMT_AMOUNT = "#,##0.00"
FMT_KRW = "#,##0"
FMT_PL_KRW = "[Red]+#,##0;[Blue]-#,##0;0"
FMT_PL = "[Red]+#,##0.00;[Blue]-#,##0.00;0.00"
FMT_PCT = "[Red]+0.00%;[Blue]-0.00%;0.00%"
FMT_DATE = "yyyy-mm-dd"
FMT_QTY = "#,##0.########"

SAMPLE_NOTE = "예시 데이터입니다(실제 시세 아님). '투자관리_시세업데이트.bat' 을 실행하면 실제 시세로 바뀝니다."


def _font(**kw) -> Font:
    return Font(name=FONT_NAME, **kw)


def _cells(ws: Worksheet, ref: str):
    """'B4' 처럼 한 칸이어도 'A1:B2' 처럼 범위여도 행 단위로 돌려줍니다."""
    if ":" not in ref:
        return ((ws[ref],),)
    return ws[ref]


def _style_range(ws: Worksheet, ref: str, **attrs) -> None:
    for row in _cells(ws, ref):
        for cell in row:
            for key, value in attrs.items():
                setattr(cell, key, value)


def _title(ws: Worksheet, text: str, subtitle: str = "") -> None:
    ws["A1"] = text
    ws["A1"].font = _font(size=16, bold=True, color=NAVY)
    if subtitle:
        ws["A2"] = subtitle
        ws["A2"].font = _font(size=9, color="5B6573")
    ws.sheet_view.showGridLines = False


def _header(ws: Worksheet, row: int, labels: Iterable[str], widths: Iterable[float] | None = None) -> None:
    for col, label in enumerate(labels, start=1):
        cell = ws.cell(row=row, column=col, value=label)
        cell.font = _font(bold=True, color="FFFFFF", size=10)
        cell.fill = HEADER_FILL
        cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
        cell.border = BOX
    ws.row_dimensions[row].height = 30
    if widths:
        for col, width in enumerate(widths, start=1):
            ws.column_dimensions[get_column_letter(col)].width = width


def _body(ws: Worksheet, ref: str, fmt: str | None = None, *, input_cell: bool = False, align: str | None = None) -> None:
    for row in _cells(ws, ref):
        for cell in row:
            cell.font = _font(size=10, color="0000FF" if input_cell else "000000")
            cell.border = BOX
            if input_cell:
                cell.fill = INPUT_FILL
            if fmt:
                cell.number_format = fmt
            if align:
                cell.alignment = Alignment(horizontal=align)


def _list_validation(ws: Worksheet, ref: str, formula: str, prompt: str) -> None:
    dv = DataValidation(type="list", formula1=formula, allow_blank=True, showDropDown=False)
    dv.promptTitle, dv.prompt = "선택", prompt
    dv.errorTitle, dv.error = "목록에서 고르세요", prompt
    dv.showInputMessage = dv.showErrorMessage = True
    ws.add_data_validation(dv)
    dv.add(ref)


def _pl_colors(ws: Worksheet, ref: str) -> None:
    ws.conditional_formatting.add(ref, CellIsRule(operator="greaterThan", formula=["0"], font=_font(color=UP_RED)))
    ws.conditional_formatting.add(ref, CellIsRule(operator="lessThan", formula=["0"], font=_font(color=DOWN_BLUE)))


def _line_props(color: str, width_emu: int = 19050, dash: str | None = None) -> GraphicalProperties:
    gp = GraphicalProperties(ln=LineProperties(solidFill=color, w=width_emu, prstDash=dash))
    return gp


# ---------------------------------------------------------------------------------------------
# 시트별 구성
# ---------------------------------------------------------------------------------------------


def _sheet_guide(ws: Worksheet) -> None:
    _title(ws, "투자관리 엑셀 — 바이낸스 · 주식", "무료로 쓰는 개인 투자 기록장. 노란 칸만 입력하면 나머지는 자동으로 계산됩니다.")
    ws.column_dimensions["A"].width = 4
    ws.column_dimensions["B"].width = 110
    lines = [
        ("처음 시작하기", True),
        ("1. [종목] 시트에 가지고 있거나 관심 있는 종목을 적습니다. (예시 줄은 지우고 쓰세요)", False),
        ("     · 바이낸스: 심볼 그대로 (BTCUSDT, ETHUSDT …) / 국내주식: 6자리 코드 (삼성전자 005930) / 해외주식: 티커 (AAPL, TSLA …)", False),
        ("2. [거래기록] 시트에 매수·매도할 때마다 한 줄씩 적습니다. 종목코드와 구분(매수/매도)은 목록에서 고르면 됩니다.", False),
        ("3. 프로젝트 폴더의 '투자관리_시세업데이트.bat' 을 더블클릭하면 무료 공개 시세로 일봉 데이터와 환율을 채웁니다.", False),
        ("     · 업데이트 전에 이 엑셀 파일을 꼭 닫아 주세요. 업데이트 전 파일은 '투자관리_백업' 폴더에 자동으로 보관됩니다.", False),
        ("4. [일봉차트] 시트 위쪽의 노란 칸에서 종목을 고르면 최근 120일 일봉 캔들차트·이동평균선·거래량이 바뀝니다.", False),
        ("", False),
        ("시트 안내", True),
        ("· 대시보드: 총 평가금액, 손익, 수익률, 시장별 비중 (모두 원화 환산)", False),
        ("· 일봉차트: 캔들(상승 빨강 / 하락 파랑) + 20일·60일 이동평균선 + 거래량", False),
        ("· 보유현황: 종목별 보유수량, 평균단가, 현재가, 평가손익, 실현손익", False),
        ("· 거래기록 / 종목 / 설정: 직접 입력하는 시트 (노란 칸)", False),
        ("· 일봉데이터: 종목코드·날짜·시가·고가·저가·종가·거래량. 자동 시세가 없는 종목은 같은 형식으로 직접 입력해도 됩니다.", False),
        ("", False),
        ("계산 방식", True),
        ("· 평균단가 = (매수금액 합계 + 매수 수수료) ÷ 매수수량 합계. 실현손익 = 매도금액 − 매도 수수료 − 매도수량 × 평균단가", False),
        ("· 수수료는 그 종목의 가격 통화로 적습니다. USDT 는 1달러로 보고 [설정]의 환율로 원화 환산합니다.", False),
        ("· 현재가 = [종목] 시트의 '수동 현재가'가 있으면 그 값, 없으면 일봉데이터의 마지막 종가", False),
        ("", False),
        ("무료 시세 출처", True),
        ("· 바이낸스 공개 API / 네이버 금융(국내주식) / 야후 파이낸스(해외주식·환율). API 키나 회원가입이 필요 없습니다.", False),
        ("· 무료 공개 데이터라 지연되거나 일시적으로 받지 못할 수 있습니다. 투자 판단의 책임은 본인에게 있습니다.", False),
        ("", False),
        ("마이크로소프트 엑셀, 무료 엑셀 웹(office.com), 리브레오피스 캘크에서 열 수 있습니다.", False),
    ]
    for i, (text, bold) in enumerate(lines, start=4):
        cell = ws.cell(row=i, column=2, value=text)
        cell.font = _font(size=11 if bold else 10, bold=bold, color=NAVY if bold else "000000")


def _sheet_settings(ws: Worksheet, inputs: Inputs, updated_at: str, data_note: str) -> None:
    _title(ws, "설정", "노란 칸만 고치세요.")
    ws.column_dimensions["A"].width = 26
    ws.column_dimensions["B"].width = 22
    ws.column_dimensions["C"].width = 70
    rows = [
        ("USD/KRW 환율", inputs.usd_krw, "달러·USDT 자산을 원화로 바꿀 때 씁니다. 자동 업데이트가 '예'면 시세 업데이트 때 바뀝니다."),
        ("환율 자동 업데이트", "예" if inputs.auto_fx else "아니오", "'아니오'로 두면 위 환율을 직접 관리합니다."),
        ("마지막 시세 업데이트", updated_at, "시세 업데이트 프로그램이 적습니다."),
        ("데이터 안내", data_note, ""),
    ]
    for i, (label, value, note) in enumerate(rows, start=4):
        ws.cell(row=i, column=1, value=label).font = _font(bold=True, size=10)
        ws.cell(row=i, column=2, value=value)
        ws.cell(row=i, column=3, value=note).font = _font(size=9, color="5B6573")
        _body(ws, f"B{i}", input_cell=i in (4, 5))
    ws["B4"].number_format = "#,##0.00"
    ws["B4"].comment = Comment("직접 입력하거나 시세 업데이트 때 야후 파이낸스 KRW=X 로 자동 갱신", "투자관리")
    _list_validation(ws, "B5", '"예,아니오"', "예 또는 아니오")
    ws["B7"].alignment = Alignment(wrap_text=True)
    ws.row_dimensions[7].height = 30


def _sheet_tickers(ws: Worksheet, tickers: list[Ticker]) -> None:
    _title(ws, "종목", "노란 칸에 종목을 적으세요 (최대 50개). 여기 적은 순서대로 보유현황에 나옵니다.")
    _header(ws, 4, ["종목코드", "종목명", "시장", "통화", "수동 현재가", "메모"], [14, 20, 12, 9, 14, 52])
    for i, t in enumerate(tickers[:TICKER_ROWS]):
        r = TICKER_FIRST + i
        ws.cell(row=r, column=1, value=t.code)
        ws.cell(row=r, column=2, value=t.name)
        ws.cell(row=r, column=3, value=t.market)
        ws.cell(row=r, column=4, value=t.currency)
        ws.cell(row=r, column=5, value=t.manual_price)
        ws.cell(row=r, column=6, value=t.memo or None)
    last = TICKER_LAST
    _body(ws, f"A{TICKER_FIRST}:D{last}", input_cell=True)
    _body(ws, f"E{TICKER_FIRST}:E{last}", FMT_PRICE, input_cell=True)
    _body(ws, f"F{TICKER_FIRST}:F{last}", input_cell=True)
    for r in range(TICKER_FIRST, last + 1):
        ws.cell(row=r, column=1).number_format = "@"  # 005930 의 앞자리 0 이 사라지지 않게
    _list_validation(ws, f"C{TICKER_FIRST}:C{last}", '"' + ",".join(MARKETS) + '"', "바이낸스 / 국내주식 / 해외주식 / 기타")
    _list_validation(ws, f"D{TICKER_FIRST}:D{last}", '"' + ",".join(CURRENCIES) + '"', "USDT / KRW / USD")
    ws["E4"].comment = Comment("자동 시세가 없는 종목만 적으세요. 값이 있으면 일봉 종가 대신 이 값을 씁니다.", "투자관리")
    ws.freeze_panes = f"A{TICKER_FIRST}"


def _sheet_trades(ws: Worksheet, trades: list[Trade]) -> None:
    _title(ws, "거래기록", "매수·매도할 때마다 한 줄씩 노란 칸에 적으세요. 회색 칸은 자동 계산됩니다.")
    _header(
        ws, 4, ["날짜", "종목코드", "구분", "수량", "단가", "수수료", "거래금액", "종목명", "시장", "메모"],
        [12, 13, 8, 14, 14, 11, 15, 18, 11, 40],
    )
    for i, t in enumerate(sorted(trades, key=lambda x: x.day)[:TRADE_ROWS]):
        r = TRADE_FIRST + i
        ws.cell(row=r, column=1, value=t.day)
        ws.cell(row=r, column=2, value=t.code)
        ws.cell(row=r, column=3, value=t.side)
        ws.cell(row=r, column=4, value=t.quantity)
        ws.cell(row=r, column=5, value=t.price)
        ws.cell(row=r, column=6, value=t.fee)
        ws.cell(row=r, column=10, value=t.memo or None)
    for r in range(TRADE_FIRST, TRADE_LAST + 1):
        ws.cell(row=r, column=7, value=f'=IF(OR(D{r}="",E{r}=""),"",D{r}*E{r})')
        lookup = f"MATCH(B{r},종목!$A${TICKER_FIRST}:$A${TICKER_LAST},0)"
        ws.cell(row=r, column=8, value=f'=IF(B{r}="","",IFERROR(INDEX(종목!$B${TICKER_FIRST}:$B${TICKER_LAST},{lookup}),"종목 없음"))')
        ws.cell(row=r, column=9, value=f'=IF(B{r}="","",IFERROR(INDEX(종목!$C${TICKER_FIRST}:$C${TICKER_LAST},{lookup}),""))')
        ws.cell(row=r, column=2).number_format = "@"
    last = TRADE_LAST
    _body(ws, f"A{TRADE_FIRST}:A{last}", FMT_DATE, input_cell=True, align="center")
    _body(ws, f"B{TRADE_FIRST}:C{last}", input_cell=True, align="center")
    _body(ws, f"D{TRADE_FIRST}:D{last}", FMT_QTY, input_cell=True)
    _body(ws, f"E{TRADE_FIRST}:F{last}", FMT_PRICE, input_cell=True)
    _body(ws, f"G{TRADE_FIRST}:G{last}", FMT_AMOUNT)
    _body(ws, f"H{TRADE_FIRST}:I{last}")
    _body(ws, f"J{TRADE_FIRST}:J{last}", input_cell=True)
    _style_range(ws, f"G{TRADE_FIRST}:I{last}", fill=PatternFill("solid", fgColor="F2F2F2"))
    for r in range(TRADE_FIRST, TRADE_LAST + 1):
        ws.cell(row=r, column=2).number_format = "@"
    _list_validation(ws, f"B{TRADE_FIRST}:B{last}", f"=종목!$A${TICKER_FIRST}:$A${TICKER_LAST}", "[종목] 시트에 적은 종목코드")
    _list_validation(ws, f"C{TRADE_FIRST}:C{last}", '"' + ",".join(SIDES) + '"', "매수 또는 매도")
    date_dv = DataValidation(type="date", operator="greaterThan", formula1="36526", allow_blank=True)
    date_dv.error, date_dv.errorTitle = "날짜를 2024-01-15 처럼 입력하세요.", "날짜 형식"
    date_dv.showErrorMessage = True
    ws.add_data_validation(date_dv)
    date_dv.add(f"A{TRADE_FIRST}:A{last}")
    ws["F4"].comment = Comment("단가와 같은 통화로 적습니다 (바이낸스는 USDT).", "투자관리")
    ws.freeze_panes = f"A{TRADE_FIRST}"
    ws.auto_filter.ref = f"A4:J{last}"


def _sheet_data(ws: Worksheet, bars_by_code: dict[str, list[Bar]]) -> None:
    _header(ws, 1, ["종목코드", "날짜", "시가", "고가", "저가", "종가", "거래량"], [13, 12, 14, 14, 14, 14, 16])
    r = 2
    for code in sorted(bars_by_code):
        for bar in sorted(bars_by_code[code], key=lambda b: b.day):
            ws.cell(row=r, column=1, value=code).number_format = "@"
            ws.cell(row=r, column=2, value=bar.day).number_format = FMT_DATE
            for col, value in enumerate((bar.open, bar.high, bar.low, bar.close), start=3):
                ws.cell(row=r, column=col, value=value).number_format = FMT_PRICE
            ws.cell(row=r, column=7, value=bar.volume).number_format = "#,##0"
            r += 1
    ws["I1"] = "직접 입력할 때: 종목코드별로 모아서, 날짜가 오래된 것부터 순서대로 적으세요 (시세 업데이트는 자동으로 정렬합니다)."
    ws["I1"].font = _font(size=9, color="5B6573")
    ws.freeze_panes = "A2"


def _sheet_holdings(ws: Worksheet) -> None:
    _title(ws, "보유현황", "모두 자동 계산됩니다. 오른쪽 '원화' 열은 [설정]의 환율로 환산한 값입니다.")
    labels = [
        "종목코드", "종목명", "시장", "통화", "매수수량", "매도수량", "보유수량", "매수원가\n(수수료 포함)",
        "평균단가", "현재가", "시세 기준일", "평가금액", "보유원가", "평가손익", "수익률", "실현손익",
        "환율", "평가금액(원)", "보유원가(원)", "평가손익(원)", "실현손익(원)", "비중",
    ]
    _header(ws, 4, labels, [12, 16, 10, 7, 12, 12, 12, 15, 13, 13, 12, 15, 15, 14, 10, 14, 9, 16, 16, 15, 15, 8])
    t = "거래기록!"
    rng = lambda col: f"{t}${col}${TRADE_FIRST}:${col}${TRADE_LAST}"  # noqa: E731
    data_code = f"일봉데이터!$A$1:$A${DATA_LAST}"
    for i in range(TICKER_ROWS):
        r = TICKER_FIRST + i
        k = TICKER_FIRST + i  # 종목 시트의 같은 행
        blank = f'$A{r}=""'
        buy = f'{rng("B")},$A{r},{rng("C")},"매수"'
        sell = f'{rng("B")},$A{r},{rng("C")},"매도"'
        last_row = f"MATCH($A{r},{data_code},0)+COUNTIF(일봉데이터!$A$2:$A${DATA_LAST},$A{r})-1"
        formulas = {
            "A": f'=IF(종목!$A{k}="","",종목!$A{k})',
            "B": f'=IF({blank},"",종목!$B{k})',
            "C": f'=IF({blank},"",종목!$C{k})',
            "D": f'=IF({blank},"",IF(종목!$D{k}="","KRW",종목!$D{k}))',
            "E": f'=IF({blank},"",SUMIFS({rng("D")},{buy}))',
            "F": f'=IF({blank},"",SUMIFS({rng("D")},{sell}))',
            "G": f'=IF({blank},"",E{r}-F{r})',
            "H": f'=IF({blank},"",SUMIFS({rng("G")},{buy})+SUMIFS({rng("F")},{buy}))',
            "I": f'=IF({blank},"",IF(E{r}=0,0,H{r}/E{r}))',
            "J": f'=IF({blank},"",IF(종목!$E{k}<>"",종목!$E{k},IFERROR(INDEX(일봉데이터!$F$1:$F${DATA_LAST},{last_row}),0)))',
            "K": f'=IF({blank},"",IF(종목!$E{k}<>"","수동",IFERROR(INDEX(일봉데이터!$B$1:$B${DATA_LAST},{last_row}),"시세 없음")))',
            "L": f'=IF({blank},"",G{r}*J{r})',
            "M": f'=IF({blank},"",G{r}*I{r})',
            "N": f'=IF({blank},"",L{r}-M{r})',
            "O": f'=IF({blank},"",IF(M{r}=0,0,N{r}/M{r}))',
            "P": f'=IF({blank},"",SUMIFS({rng("G")},{sell})-SUMIFS({rng("F")},{sell})-F{r}*I{r})',
            "Q": f'=IF({blank},"",IF(D{r}="KRW",1,설정!$B$4))',
            "R": f'=IF({blank},"",L{r}*Q{r})',
            "S": f'=IF({blank},"",M{r}*Q{r})',
            "T": f'=IF({blank},"",N{r}*Q{r})',
            "U": f'=IF({blank},"",P{r}*Q{r})',
            "V": f'=IF(OR({blank},$R${TICKER_LAST + 2}=0),"",R{r}/$R${TICKER_LAST + 2})',
        }
        for col, formula in formulas.items():
            ws[f"{col}{r}"] = formula
    first, last, total = TICKER_FIRST, TICKER_LAST, TICKER_LAST + 2
    _body(ws, f"A{first}:D{last}", align="center")
    _body(ws, f"B{first}:B{last}")
    _body(ws, f"E{first}:G{last}", FMT_QTY)
    _body(ws, f"H{first}:H{last}", FMT_AMOUNT)
    _body(ws, f"I{first}:J{last}", FMT_PRICE)
    _body(ws, f"K{first}:K{last}", FMT_DATE, align="center")
    _body(ws, f"L{first}:M{last}", FMT_AMOUNT)
    _body(ws, f"N{first}:N{last}", FMT_PL)
    _body(ws, f"O{first}:O{last}", FMT_PCT)
    _body(ws, f"P{first}:P{last}", FMT_PL)
    _body(ws, f"Q{first}:Q{last}", "#,##0.00")
    _body(ws, f"R{first}:S{last}", FMT_KRW)
    _body(ws, f"T{first}:U{last}", FMT_PL_KRW)
    _body(ws, f"V{first}:V{last}", "0.0%")

    ws[f"A{total}"] = "합계 (원)"
    for col in "RSTU":
        ws[f"{col}{total}"] = f"=SUM({col}{first}:{col}{last})"
    ws[f"O{total}"] = f"=IF(S{total}=0,0,T{total}/S{total})"
    ws[f"V{total}"] = f'=IF(R{total}=0,"",1)'
    _body(ws, f"A{total}:V{total}")
    _style_range(ws, f"A{total}:V{total}", fill=TOTAL_FILL, font=_font(bold=True, size=10))
    ws[f"R{total}"].number_format = ws[f"S{total}"].number_format = FMT_KRW
    ws[f"T{total}"].number_format = ws[f"U{total}"].number_format = FMT_PL_KRW
    ws[f"O{total}"].number_format = FMT_PCT
    ws[f"V{total}"].number_format = "0%"
    ws.freeze_panes = f"C{first}"


BLOCK_COLS = 11  # 차트데이터: 종목 하나당 열 묶음 너비
BLOCK_ROWS = 40  # 일봉차트: 종목 하나당 행 수
CHART_TOP = 6  # 일봉차트: 첫 종목 블록 시작 행


def _block_col(index: int) -> int:
    return 1 + index * BLOCK_COLS


def _sheet_chart_data(ws: Worksheet, codes: list[str]) -> None:
    """종목마다 최근 120일 일봉·거래량·이동평균을 일봉데이터에서 끌어오는 계산 표 (숨김 시트)."""
    d = lambda col: f"일봉데이터!${col}$1:${col}${DATA_LAST}"  # noqa: E731
    for index, code in enumerate(codes):
        c0 = _block_col(index)
        L = lambda offset, c0=c0: get_column_letter(c0 + offset)  # noqa: E731
        code_ref, first_ref, count_ref = f"${L(1)}$1", f"${L(1)}$2", f"${L(1)}$3"
        ws.cell(row=1, column=c0, value="종목")
        ws.cell(row=1, column=c0 + 1, value=code).number_format = "@"
        ws.cell(row=2, column=c0, value="첫 행")
        ws.cell(row=2, column=c0 + 1, value=f"=IFERROR(MATCH({code_ref},{d('A')},0),0)")
        ws.cell(row=3, column=c0, value="일수")
        ws.cell(row=3, column=c0 + 1, value=f"=COUNTIF(일봉데이터!$A$2:$A${DATA_LAST},{code_ref})")
        ws.cell(row=1, column=c0 + 3, value="구간 시작 행")
        ws.cell(row=1, column=c0 + 4, value=f"=IF({count_ref}=0,0,MAX({first_ref},{first_ref}+{count_ref}-{CHART_DAYS}))")
        ws.cell(row=2, column=c0 + 3, value="구간 끝 행")
        ws.cell(row=2, column=c0 + 4, value=f"=IF({count_ref}=0,0,{first_ref}+{count_ref}-1)")
        labels = ["행번호", "날짜", "시가", "고가", "저가", "종가", "상승 거래량", "하락 거래량", "20일 이동평균", "60일 이동평균"]
        for offset, label in enumerate(labels):
            cell = ws.cell(row=CHART_FIRST - 1, column=c0 + offset, value=label)
            cell.font = _font(bold=True, color="FFFFFF", size=9)
            cell.fill = HEADER_FILL
        for r in range(CHART_FIRST, CHART_LAST + 1):
            row_ref = f"${L(0)}{r}"
            ok = f"AND({first_ref}>0,{row_ref}>={first_ref})"
            ws[f"{L(0)}{r}"] = f"=IF({count_ref}=0,0,{first_ref}+{count_ref}-{CHART_DAYS}+{r - CHART_FIRST})"
            ws[f"{L(1)}{r}"] = f'=IF({ok},INDEX({d("B")},{row_ref}),"")'
            for offset, col in zip((2, 3, 4, 5), "CDEF"):
                ws[f"{L(offset)}{r}"] = f"=IF({ok},INDEX({d(col)},{row_ref}),NA())"
            ws[f"{L(6)}{r}"] = f"=IF({ok},IF({L(5)}{r}>={L(2)}{r},INDEX({d('G')},{row_ref}),0),0)"
            ws[f"{L(7)}{r}"] = f"=IF({ok},IF({L(5)}{r}<{L(2)}{r},INDEX({d('G')},{row_ref}),0),0)"
            for offset, n in ((8, 20), (9, 60)):
                ws[f"{L(offset)}{r}"] = (
                    f"=IF(AND({first_ref}>0,{row_ref}-{n - 1}>={first_ref}),"
                    f"AVERAGE(INDEX({d('F')},{row_ref}-{n - 1}):INDEX({d('F')},{row_ref})),NA())"
                )
            ws[f"{L(1)}{r}"].number_format = "mm-dd"
    if not codes:
        ws["A1"] = "차트로 그릴 종목이 없습니다."


def _nice_axis(bars: list[Bar]) -> tuple[float, float, float]:
    """최근 120일 고가·저가와 이동평균이 화면에 꽉 차도록 세로축 범위를 정합니다."""
    window = bars[-CHART_DAYS:]
    closes = [b.close for b in bars]
    values = [b.high for b in window] + [b.low for b in window]
    for n in (20, 60):
        for i in range(max(n - 1, len(bars) - CHART_DAYS), len(bars)):
            values.append(sum(closes[i - n + 1 : i + 1]) / n)
    lo, hi = min(values), max(values)
    span = (hi - lo) or abs(hi) * 0.1 or 1.0
    lo, hi = lo - span * 0.06, hi + span * 0.06
    raw = (hi - lo) / 6
    magnitude = 10 ** math.floor(math.log10(raw))
    step = next(m * magnitude for m in (1, 2, 2.5, 5, 10) if m * magnitude >= raw)
    low = max(0.0, math.floor(lo / step) * step)
    high = math.ceil(hi / step) * step
    return low, high, step


def _sheet_chart(ws: Worksheet, tickers: list[Ticker], bars_by_code: dict[str, list[Bar]], data_ws: Worksheet) -> list[str]:
    """종목마다 캔들차트 + 거래량 차트 한 묶음. 차트를 그린 종목코드 목록을 돌려줍니다."""
    _title(ws, "일봉 차트", "종목별 최근 120일 일봉 · 20일/60일 이동평균 · 거래량 (상승 빨강 / 하락 파랑). 위 링크를 누르면 그 종목으로 이동합니다.")
    for col, width in zip("ABCDEFGHIJKLMNOP", (2, 13, 15, 2, 13, 15, 2, 13, 15, 2, 13, 15, 2, 13, 15, 2)):
        ws.column_dimensions[col].width = width
    codes = [t.code for t in tickers if bars_by_code.get(t.code)]
    names = {t.code: t.name for t in tickers}
    if not codes:
        ws["B4"] = "아직 일봉 데이터가 없습니다. '투자관리_시세업데이트.bat' 을 실행하거나 [일봉데이터] 시트에 직접 입력하세요."
        ws["B4"].font = _font(size=11, color=DOWN_BLUE)
        return codes

    link_cols = ("B", "C", "E", "F", "H", "I", "K", "L", "N", "O")
    for i, code in enumerate(codes[: len(link_cols) * 2]):
        cell = ws[f"{link_cols[i % len(link_cols)]}{3 + i // len(link_cols)}"]
        cell.value = f"▶ {names.get(code) or code}"
        cell.hyperlink = f"#'일봉차트'!B{CHART_TOP + i * BLOCK_ROWS}"
        cell.font = _font(size=10, color="2F5BD3", underline="single")

    _sheet_chart_data(data_ws, codes)
    for index, code in enumerate(codes):
        top = CHART_TOP + index * BLOCK_ROWS
        c0 = _block_col(index)
        L = lambda offset, c0=c0: get_column_letter(c0 + offset)  # noqa: E731
        cd = "차트데이터!"
        start_ref, end_ref = f"{cd}${L(4)}$1", f"{cd}${L(4)}$2"
        close, prev = f"{cd}${L(5)}${CHART_LAST}", f"{cd}${L(5)}${CHART_LAST - 1}"
        span = lambda col, s=start_ref, e=end_ref: f"INDEX(일봉데이터!${col}$1:${col}${DATA_LAST},{s}):INDEX(일봉데이터!${col}$1:${col}${DATA_LAST},{e})"  # noqa: E731

        ws[f"B{top}"] = f"{names.get(code) or code}  ({code})"
        ws[f"B{top}"].font = _font(size=13, bold=True, color=NAVY)
        ws[f"O{top}"] = "▲ 맨 위로"
        ws[f"O{top}"].hyperlink = "#'일봉차트'!A1"
        ws[f"O{top}"].font = _font(size=9, color="2F5BD3", underline="single")
        for col in "BCDEFGHIJKLMNO":
            ws[f"{col}{top}"].border = Border(bottom=Side(style="medium", color=NAVY))
        stats = [
            (1, "B", "최근 일자", f'=IFERROR(INDEX(일봉데이터!$B$1:$B${DATA_LAST},{end_ref}),"-")', FMT_DATE),
            (1, "E", "종가", f'=IFERROR({close},"-")', FMT_PRICE),
            (1, "H", "전일 대비", f'=IFERROR({close}-{prev},"-")', "[Red]+#,##0.00##;[Blue]-#,##0.00##;0"),
            (1, "K", "등락률", f'=IFERROR({close}/{prev}-1,"-")', FMT_PCT),
            (1, "N", "120일 평균 거래량", f'=IFERROR(AVERAGE({span("G")}),"-")', "#,##0"),
            (2, "B", "120일 최고가", f'=IFERROR(MAX({span("D")}),"-")', FMT_PRICE),
            (2, "E", "120일 최저가", f'=IFERROR(MIN({span("E")}),"-")', FMT_PRICE),
            (2, "H", "20일 이동평균", f'=IFERROR({cd}${L(8)}${CHART_LAST},"-")', FMT_PRICE),
            (2, "K", "60일 이동평균", f'=IFERROR({cd}${L(9)}${CHART_LAST},"-")', FMT_PRICE),
            (2, "N", "표시 일수", f"=IF({end_ref}=0,0,{end_ref}-{start_ref}+1)", "0"),
        ]
        for dr, col, label, formula, fmt in stats:
            r = top + dr
            value_col = get_column_letter(ws[f"{col}1"].column + 1)
            ws[f"{col}{r}"] = label
            ws[f"{col}{r}"].font = _font(size=9, color="5B6573")
            cell = ws[f"{value_col}{r}"]
            cell.value = formula
            cell.font = _font(bold=True, size=11)
            cell.number_format = fmt
            cell.alignment = Alignment(horizontal="right")
        for ref in (f"I{top + 1}", f"L{top + 1}"):
            _pl_colors(ws, ref)

        low, high, step = _nice_axis(sorted(bars_by_code[code], key=lambda b: b.day))
        ws.add_chart(_candle_chart(data_ws, c0, low, high, step), f"B{top + 4}")
        ws.add_chart(_volume_chart(data_ws, c0), f"B{top + 27}")
    ws.freeze_panes = "A5"
    return codes


def _candle_chart(data_ws: Worksheet, c0: int, low: float, high: float, step: float) -> StockChart:
    stock = StockChart()
    stock.add_data(Reference(data_ws, min_col=c0 + 2, max_col=c0 + 5, min_row=CHART_FIRST - 1, max_row=CHART_LAST), titles_from_data=True)
    stock.set_categories(Reference(data_ws, min_col=c0 + 1, min_row=CHART_FIRST, max_row=CHART_LAST))
    for series in stock.series:
        series.graphicalProperties.line.noFill = True
        series.marker = Marker(symbol="none")
        series.smooth = False
    stock.hiLowLines = ChartLines(spPr=GraphicalProperties(ln=LineProperties(solidFill="595959", w=9525)))
    stock.upDownBars = UpDownBars(
        gapWidth=60,
        upBars=ChartLines(spPr=GraphicalProperties(solidFill=UP_RED, ln=LineProperties(solidFill=UP_RED, w=6350))),
        downBars=ChartLines(spPr=GraphicalProperties(solidFill=DOWN_BLUE, ln=LineProperties(solidFill=DOWN_BLUE, w=6350))),
    )

    averages = LineChart()
    averages.add_data(Reference(data_ws, min_col=c0 + 8, max_col=c0 + 9, min_row=CHART_FIRST - 1, max_row=CHART_LAST), titles_from_data=True)
    for series, color in zip(averages.series, ("F39C12", "8E44AD")):
        series.graphicalProperties = _line_props(color, 19050)
        series.marker = Marker(symbol="none")
        series.smooth = False
    stock += averages

    stock.title = None
    stock.height, stock.width = 11.5, 27
    stock.legend = Legend(legendPos="t")
    stock.y_axis.scaling.min = low
    stock.y_axis.scaling.max = high
    stock.y_axis.majorUnit = step
    stock.y_axis.number_format = "#,##0.####"
    stock.y_axis.majorGridlines = ChartLines(spPr=GraphicalProperties(ln=LineProperties(solidFill="E5E8EC", w=6350)))
    stock.y_axis.delete = False
    stock.x_axis.delete = False
    stock.x_axis.number_format = "mm-dd"
    stock.x_axis.tickLblSkip = 20
    stock.x_axis.tickMarkSkip = 10
    stock.x_axis.tickLblPos = "low"
    return stock


def _volume_chart(data_ws: Worksheet, c0: int) -> BarChart:
    volume = BarChart()
    volume.type = "col"
    volume.grouping = "stacked"
    volume.overlap = 100
    volume.gapWidth = 60
    volume.add_data(Reference(data_ws, min_col=c0 + 6, max_col=c0 + 7, min_row=CHART_FIRST - 1, max_row=CHART_LAST), titles_from_data=True)
    volume.set_categories(Reference(data_ws, min_col=c0 + 1, min_row=CHART_FIRST, max_row=CHART_LAST))
    for series, color in zip(volume.series, (UP_RED, DOWN_BLUE)):
        series.graphicalProperties = GraphicalProperties(solidFill=color, ln=LineProperties(solidFill=color))
    volume.title = None
    volume.height, volume.width = 5.5, 27
    volume.legend = None
    volume.y_axis.title = "거래량"
    volume.y_axis.number_format = "#,##0"
    volume.y_axis.majorGridlines = ChartLines(spPr=GraphicalProperties(ln=LineProperties(solidFill="E5E8EC", w=6350)))
    volume.y_axis.delete = False
    volume.x_axis.delete = False
    volume.x_axis.number_format = "mm-dd"
    volume.x_axis.tickLblSkip = 20
    volume.x_axis.tickMarkSkip = 10
    return volume


def _sheet_dashboard(ws: Worksheet) -> None:
    _title(ws, "투자 대시보드", "모든 금액은 원화 환산 기준입니다. 숫자는 [보유현황]에서 자동으로 계산됩니다.")
    for col, width in zip("ABCDEFGH", (2, 20, 18, 18, 18, 14, 2, 20)):
        ws.column_dimensions[col].width = width
    total = TICKER_LAST + 2
    h = "보유현황!"
    tiles = [
        ("B4", "총 평가금액", f"={h}$R${total}", FMT_KRW),
        ("C4", "총 보유원가", f"={h}$S${total}", FMT_KRW),
        ("D4", "평가손익", f"={h}$T${total}", FMT_PL_KRW),
        ("E4", "수익률", f"={h}$O${total}", FMT_PCT),
        ("F4", "실현손익", f"={h}$U${total}", FMT_PL_KRW),
    ]
    for ref, label, formula, fmt in tiles:
        col = ref[0]
        ws[ref] = label
        ws[ref].font = _font(size=9, bold=True, color="FFFFFF")
        ws[ref].fill = HEADER_FILL
        ws[ref].alignment = Alignment(horizontal="center")
        value = ws[f"{col}5"]
        value.value = formula
        value.number_format = fmt
        value.font = _font(size=14, bold=True)
        value.alignment = Alignment(horizontal="center", vertical="center")
        value.border = BOX
    ws.column_dimensions["F"].width = 18
    ws.row_dimensions[5].height = 34
    _pl_colors(ws, "D5:F5")
    ws["H4"], ws["H5"] = "USD/KRW 환율", "=설정!$B$4"
    ws["H4"].font = _font(size=9, color="5B6573")
    ws["H5"].number_format = "#,##0.00"
    ws["H5"].font = _font(size=11, bold=True)
    ws["H6"], ws["H7"] = "마지막 시세 업데이트", "=설정!$B$6"
    ws["H6"].font = _font(size=9, color="5B6573")
    ws["H7"].font = _font(size=10)

    _header_at(ws, 8, 2, ["시장", "평가금액(원)", "보유원가(원)", "평가손익(원)", "수익률"])
    for i, market in enumerate(MARKETS):
        r = 9 + i
        ws[f"B{r}"] = market
        ws[f"C{r}"] = f"=SUMIFS({h}$R${TICKER_FIRST}:$R${TICKER_LAST},{h}$C${TICKER_FIRST}:$C${TICKER_LAST},B{r})"
        ws[f"D{r}"] = f"=SUMIFS({h}$S${TICKER_FIRST}:$S${TICKER_LAST},{h}$C${TICKER_FIRST}:$C${TICKER_LAST},B{r})"
        ws[f"E{r}"] = f"=C{r}-D{r}"
        ws[f"F{r}"] = f"=IF(D{r}=0,0,E{r}/D{r})"
    end = 9 + len(MARKETS) - 1
    ws[f"B{end + 1}"] = "합계"
    for col in "CDE":
        ws[f"{col}{end + 1}"] = f"=SUM({col}9:{col}{end})"
    ws[f"F{end + 1}"] = f"=IF(D{end + 1}=0,0,E{end + 1}/D{end + 1})"
    _body(ws, f"B9:B{end + 1}", align="center")
    _body(ws, f"C9:D{end + 1}", FMT_KRW)
    _body(ws, f"E9:E{end + 1}", FMT_PL_KRW)
    _body(ws, f"F9:F{end + 1}", FMT_PCT)
    _style_range(ws, f"B{end + 1}:F{end + 1}", fill=TOTAL_FILL)

    pie = PieChart()
    pie.add_data(Reference(ws, min_col=3, min_row=8, max_row=end), titles_from_data=True)
    pie.set_categories(Reference(ws, min_col=2, min_row=9, max_row=end))
    pie.title = "시장별 평가금액 비중"
    pie.dataLabels = DataLabelList(showPercent=True, showCatName=True, showVal=False, showSerName=False, showLeaderLines=True)
    pie.height, pie.width = 8.5, 12
    ws.add_chart(pie, "B16")

    bars = BarChart()
    bars.type = "col"
    bars.add_data(Reference(ws, min_col=3, max_col=4, min_row=8, max_row=end), titles_from_data=True)
    bars.set_categories(Reference(ws, min_col=2, min_row=9, max_row=end))
    bars.title = "시장별 평가금액 vs 원가"
    for series, color in zip(bars.series, ("2F5BD3", "A0AEC0")):
        series.graphicalProperties = GraphicalProperties(solidFill=color, ln=LineProperties(solidFill=color))
    bars.y_axis.number_format = "#,##0"
    bars.y_axis.delete = False
    bars.x_axis.delete = False
    bars.legend = Legend(legendPos="b")
    bars.height, bars.width = 8.5, 14
    ws.add_chart(bars, "E16")


def _header_at(ws: Worksheet, row: int, col: int, labels: list[str]) -> None:
    for i, label in enumerate(labels):
        cell = ws.cell(row=row, column=col + i, value=label)
        cell.font = _font(bold=True, color="FFFFFF", size=10)
        cell.fill = HEADER_FILL
        cell.alignment = Alignment(horizontal="center")
        cell.border = BOX


# ---------------------------------------------------------------------------------------------
# 공개 함수
# ---------------------------------------------------------------------------------------------


def build_workbook(inputs: Inputs, bars_by_code: dict[str, list[Bar]], *, updated_at: str = "-", data_note: str = "") -> Workbook:
    wb = Workbook()
    guide = wb.active
    guide.title = "사용법"
    dashboard = wb.create_sheet("대시보드")
    chart = wb.create_sheet("일봉차트")
    holdings = wb.create_sheet("보유현황")
    trades = wb.create_sheet("거래기록")
    tickers = wb.create_sheet("종목")
    settings = wb.create_sheet("설정")
    data = wb.create_sheet("일봉데이터")
    chart_data = wb.create_sheet("차트데이터")

    _sheet_guide(guide)
    _sheet_settings(settings, inputs, updated_at, data_note)
    _sheet_tickers(tickers, inputs.tickers)
    _sheet_trades(trades, inputs.trades)
    _sheet_data(data, bars_by_code)
    _sheet_holdings(holdings)
    _sheet_chart(chart, inputs.tickers, bars_by_code, chart_data)
    _sheet_dashboard(dashboard)
    chart_data.sheet_state = "hidden"

    for ws, color in ((dashboard, NAVY), (chart, NAVY), (trades, "BF8F00"), (tickers, "BF8F00"), (settings, "BF8F00")):
        ws.sheet_properties.tabColor = color
    for ws in wb.worksheets:
        ws.page_setup.orientation = "landscape"
        ws.page_setup.fitToWidth = 1
        ws.page_setup.fitToHeight = 0
        ws.sheet_properties.pageSetUpPr.fitToPage = True
    wb.active = 1  # 대시보드부터 보이게
    wb.calculation.fullCalcOnLoad = True  # 엑셀이 열 때 모든 수식을 다시 계산
    return wb


def _text(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    return str(value).strip()


def _number(value: object, default: float | None = None) -> float | None:
    if value is None or value == "":
        return default
    try:
        return float(str(value).replace(",", ""))
    except ValueError:
        return default


def _day(value: object) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        for fmt in ("%Y-%m-%d", "%Y.%m.%d", "%Y/%m/%d", "%Y%m%d"):
            try:
                return datetime.strptime(value.strip(), fmt).date()
            except ValueError:
                continue
    return None


def read_workbook(path: Path) -> tuple[Inputs, dict[str, list[Bar]], list[str]]:
    """입력 시트(설정·종목·거래기록·차트 선택)와 일봉데이터를 읽습니다. 세 번째 값은 건너뛴 줄 안내."""
    wb = load_workbook(path, data_only=False)
    warnings: list[str] = []
    inputs = Inputs()

    if "설정" in wb.sheetnames:
        s = wb["설정"]
        inputs.usd_krw = _number(s["B4"].value, inputs.usd_krw) or inputs.usd_krw
        inputs.auto_fx = _text(s["B5"].value) != "아니오"
    if "종목" in wb.sheetnames:
        ws = wb["종목"]
        for r in range(TICKER_FIRST, TICKER_LAST + 1):
            code = _text(ws.cell(row=r, column=1).value)
            if not code:
                continue
            market = _text(ws.cell(row=r, column=3).value) or "기타"
            if market not in MARKETS:
                warnings.append(f"[종목] {r}행 '{code}': 시장 '{market}' 을 알 수 없어 '기타'로 봅니다.")
                market = "기타"
            currency = _text(ws.cell(row=r, column=4).value) or DEFAULT_CURRENCY[market]
            inputs.tickers.append(
                Ticker(
                    code=code,
                    name=_text(ws.cell(row=r, column=2).value),
                    market=market,
                    currency=currency,
                    manual_price=_number(ws.cell(row=r, column=5).value),
                    memo=_text(ws.cell(row=r, column=6).value),
                )
            )

    if "거래기록" in wb.sheetnames:
        ws = wb["거래기록"]
        for r in range(TRADE_FIRST, ws.max_row + 1):
            raw = [ws.cell(row=r, column=c).value for c in (1, 2, 3, 4, 5, 6, 10)]
            if all(v in (None, "") for v in raw[:6]):
                continue
            day, code, side = _day(raw[0]), _text(raw[1]), _text(raw[2])
            qty, price, fee = _number(raw[3]), _number(raw[4]), _number(raw[5], 0.0)
            if day is None or not code or side not in SIDES or qty is None or price is None:
                warnings.append(f"[거래기록] {r}행: 날짜·종목코드·구분·수량·단가 중 빈 칸이나 잘못된 값이 있어 건너뜁니다.")
                continue
            inputs.trades.append(Trade(day, code, side, qty, price, fee or 0.0, _text(raw[6])))

    bars: dict[str, list[Bar]] = {}
    if "일봉데이터" in wb.sheetnames:
        ws = wb["일봉데이터"]
        for row in ws.iter_rows(min_row=2, max_col=7, values_only=True):
            code, day = _text(row[0]), _day(row[1])
            values = [_number(v) for v in row[2:7]]
            if not code or day is None or any(v is None for v in values[:4]):
                continue
            bars.setdefault(code, []).append(Bar(day, *values[:4], values[4] or 0.0))
    return inputs, bars, warnings
