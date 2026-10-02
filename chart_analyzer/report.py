"""분석 결과를 HTML / Markdown / JSON 보고서 파일로 저장합니다."""

from __future__ import annotations

import base64
import json
import re
from dataclasses import dataclass
from datetime import datetime
from html import escape
from pathlib import Path

from .analyzer import AnalysisResult
from .imaging import PreparedImage
from .schema import ChartAnalysis

BIAS_LABEL = {
    "strong_bullish": "강한 상승",
    "bullish": "상승 우위",
    "neutral": "중립",
    "bearish": "하락 우위",
    "strong_bearish": "강한 하락",
}
SIGNAL_LABEL = {"bullish": "상승", "bearish": "하락", "neutral": "중립", "sideways": "횡보"}
GRADE_LABEL = {"high": "높음", "medium": "보통", "low": "낮음"}
LEVEL_LABEL = {"support": "지지", "resistance": "저항"}
PATTERN_STATUS_LABEL = {"forming": "형성 중", "confirmed": "완성", "failed": "실패"}
SCENARIO_LABEL = {"bullish": "상승 시나리오", "bearish": "하락 시나리오", "sideways": "횡보 시나리오"}

DISCLAIMER = "이 보고서는 AI 가 이미지 한 장을 보고 만든 기술적 분석 참고 자료이며 투자 조언이 아닙니다. 판단과 책임은 본인에게 있습니다."


@dataclass(frozen=True)
class SavedReport:
    html: Path
    markdown: Path
    json: Path
    image: Path


def _tone(value: str) -> str:
    if "bullish" in value:
        return "up"
    if "bearish" in value:
        return "down"
    return "flat"


def _slug(text: str | None) -> str:
    cleaned = re.sub(r"[^0-9A-Za-z가-힣]+", "_", text or "").strip("_")
    return cleaned[:40]


def report_stem(analysis: ChartAnalysis, created: datetime) -> str:
    parts = [created.strftime("%Y%m%d-%H%M%S"), _slug(analysis.instrument), _slug(analysis.timeframe)]
    return "_".join(p for p in parts if p) or created.strftime("%Y%m%d-%H%M%S")


def _meta_line(a: ChartAnalysis) -> list[str]:
    items = [a.instrument or "종목 판독 불가", a.timeframe or "봉 간격 판독 불가", a.chart_type]
    if a.current_price:
        items.append(f"현재가 {a.current_price}")
    return items


# ---------------------------------------------------------------------------------------------
# HTML
# ---------------------------------------------------------------------------------------------

_CSS = """
:root {
  --bg: #f6f7f9; --card: #ffffff; --text: #1b1f24; --muted: #5b6573; --line: #e3e6ea;
  --up: #0f8a5f; --up-bg: #e3f5ec; --down: #c8382e; --down-bg: #fbe7e5; --flat: #5b6573; --flat-bg: #eceff2;
  --accent: #2f5bd3;
}
@media (prefers-color-scheme: dark) {
  :root {
    --bg: #111418; --card: #1a1e24; --text: #e7eaee; --muted: #9aa4b1; --line: #2a3038;
    --up: #3ccf91; --up-bg: #15302a; --down: #ff6b5f; --down-bg: #3a1d1b; --flat: #aab3bf; --flat-bg: #262c34;
    --accent: #7ea2ff;
  }
}
* { box-sizing: border-box; }
body { margin: 0; background: var(--bg); color: var(--text);
  font-family: "Pretendard", "Malgun Gothic", "Apple SD Gothic Neo", system-ui, sans-serif; line-height: 1.6; }
main { max-width: 980px; margin: 0 auto; padding: 24px 16px 48px; }
header h1 { font-size: 1.6rem; margin: 0 0 6px; line-height: 1.3; }
.meta { color: var(--muted); font-size: .92rem; display: flex; flex-wrap: wrap; gap: 4px 14px; }
section { background: var(--card); border: 1px solid var(--line); border-radius: 12px; padding: 18px 20px; margin-top: 16px; }
h2 { font-size: 1.1rem; margin: 0 0 12px; }
.badges { display: flex; flex-wrap: wrap; gap: 8px; margin-bottom: 12px; }
.badge { display: inline-block; padding: 3px 10px; border-radius: 999px; font-size: .85rem; font-weight: 600; }
.up { color: var(--up); background: var(--up-bg); }
.down { color: var(--down); background: var(--down-bg); }
.flat { color: var(--flat); background: var(--flat-bg); }
.summary li { margin: 4px 0; font-size: 1.02rem; }
figure { margin: 0; }
figure img { width: 100%; height: auto; border-radius: 8px; border: 1px solid var(--line); display: block; }
figcaption { color: var(--muted); font-size: .85rem; margin-top: 6px; }
.grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(260px, 1fr)); gap: 12px; }
.box { border: 1px solid var(--line); border-radius: 10px; padding: 12px 14px; }
.box h3 { font-size: 1rem; margin: 0 0 6px; display: flex; gap: 8px; align-items: center; flex-wrap: wrap; }
.box p { margin: 4px 0; }
.label { color: var(--muted); font-size: .85rem; margin-right: 4px; }
table { width: 100%; border-collapse: collapse; font-size: .95rem; }
th, td { text-align: left; padding: 8px 6px; border-bottom: 1px solid var(--line); vertical-align: top; }
th { color: var(--muted); font-weight: 600; font-size: .85rem; }
td.num { font-variant-numeric: tabular-nums; white-space: nowrap; font-weight: 600; }
.table-wrap { overflow-x: auto; }
ul { padding-left: 1.2em; margin: 0; }
a { color: var(--accent); }
.muted { color: var(--muted); }
.disclaimer { color: var(--muted); font-size: .85rem; margin-top: 20px; }
@media print { body { background: #fff; } section { break-inside: avoid; } }
"""


def _badge(text: str, tone: str) -> str:
    return f'<span class="badge {tone}">{escape(text)}</span>'


def _list(items: list[str], empty: str = "없음") -> str:
    if not items:
        return f'<p class="muted">{escape(empty)}</p>'
    return "<ul>" + "".join(f"<li>{escape(i)}</li>" for i in items) + "</ul>"


def render_html(result: AnalysisResult, image: PreparedImage, created: datetime, source: str) -> str:
    a = result.analysis
    img_src = f"data:{image.media_type};base64,{base64.standard_b64encode(image.data).decode('ascii')}"
    parts: list[str] = []

    parts.append(
        "<header>"
        f"<h1>{escape(a.title)}</h1>"
        '<div class="meta">'
        + "".join(f"<span>{escape(m)}</span>" for m in _meta_line(a))
        + f"<span>작성 {escape(created.strftime('%Y-%m-%d %H:%M'))}</span>"
        "</div></header>"
    )

    parts.append(
        "<section><h2>핵심 요약</h2>"
        '<div class="badges">'
        + _badge(f"종합 판단: {BIAS_LABEL[a.overall_bias]}", _tone(a.overall_bias))
        + _badge(f"확신도: {GRADE_LABEL[a.confidence]}", "flat")
        + "</div>"
        f'<ul class="summary">{"".join(f"<li>{escape(s)}</li>" for s in a.summary)}</ul>'
        "</section>"
    )

    parts.append(
        f'<section><h2>분석한 차트</h2><figure><img src="{img_src}" alt="분석한 차트 캡처">'
        f"<figcaption>출처: {escape(source)} · {image.width}×{image.height}px</figcaption></figure></section>"
    )

    if not a.is_chart:
        parts.append(f"<section><h2>판독 한계</h2>{_list(a.limitations)}</section>")
        return _wrap_html(a.title, "".join(parts), result)

    t = a.trend
    parts.append(
        "<section><h2>추세</h2><div class=\"grid\">"
        f'<div class="box"><h3>단기 {_badge(SIGNAL_LABEL[t.short_term], _tone(t.short_term))}</h3>'
        f"<p>{escape(t.short_term_comment)}</p></div>"
        f'<div class="box"><h3>중기 {_badge(SIGNAL_LABEL[t.medium_term], _tone(t.medium_term))}</h3>'
        f"<p>{escape(t.medium_term_comment)}</p></div>"
        f'</div><p style="margin-top:12px"><span class="label">구조</span>{escape(t.structure)}</p></section>'
    )

    if a.key_levels:
        rows = "".join(
            "<tr>"
            f"<td>{_badge(LEVEL_LABEL[lv.kind], 'up' if lv.kind == 'support' else 'down')}</td>"
            f'<td class="num">{escape(lv.price)}</td>'
            f"<td>{escape(GRADE_LABEL[lv.strength])}</td>"
            f"<td>{escape(lv.basis)}</td>"
            "</tr>"
            for lv in a.key_levels
        )
        parts.append(
            "<section><h2>주요 가격대</h2><div class=\"table-wrap\"><table>"
            "<thead><tr><th>구분</th><th>가격</th><th>강도</th><th>근거</th></tr></thead>"
            f"<tbody>{rows}</tbody></table></div></section>"
        )

    if a.patterns:
        boxes = "".join(
            f'<div class="box"><h3>{escape(p.name)} {_badge(PATTERN_STATUS_LABEL[p.status], "flat")}'
            f" {_badge(SIGNAL_LABEL[p.signal], _tone(p.signal))}</h3><p>{escape(p.description)}</p></div>"
            for p in a.patterns
        )
        parts.append(f'<section><h2>패턴</h2><div class="grid">{boxes}</div></section>')

    if a.indicators or a.volume_analysis:
        body = ""
        if a.indicators:
            rows = "".join(
                f"<tr><td>{escape(i.name)}</td><td>{escape(i.reading)}</td>"
                f"<td>{_badge(SIGNAL_LABEL[i.signal], _tone(i.signal))}</td></tr>"
                for i in a.indicators
            )
            body += (
                '<div class="table-wrap"><table><thead><tr><th>지표</th><th>판독</th><th>신호</th></tr></thead>'
                f"<tbody>{rows}</tbody></table></div>"
            )
        if a.volume_analysis:
            body += f'<p style="margin-top:12px"><span class="label">거래량</span>{escape(a.volume_analysis)}</p>'
        parts.append(f"<section><h2>보조지표 · 거래량</h2>{body}</section>")

    if a.scenarios:
        boxes = "".join(
            f'<div class="box"><h3>{escape(SCENARIO_LABEL[s.direction])}'
            f" {_badge('가능성 ' + GRADE_LABEL[s.likelihood], _tone(s.direction))}</h3>"
            f"<p>{escape(s.description)}</p>"
            f'<p><span class="label">조건</span>{escape(s.trigger)}</p>'
            f'<p><span class="label">목표</span>{escape(" → ".join(s.targets) or "-")}</p>'
            f'<p><span class="label">무효화</span>{escape(s.invalidation)}</p></div>'
            for s in a.scenarios
        )
        parts.append(f'<section><h2>시나리오</h2><div class="grid">{boxes}</div></section>')

    parts.append(
        '<section><h2>리스크 · 체크포인트</h2><div class="grid">'
        f'<div class="box"><h3>주의할 위험</h3>{_list(a.risks)}</div>'
        f'<div class="box"><h3>다음에 확인할 것</h3>{_list(a.watch_points)}</div>'
        "</div></section>"
    )
    parts.append(f"<section><h2>판독 한계</h2>{_list(a.limitations)}</section>")
    return _wrap_html(a.title, "".join(parts), result)


def _wrap_html(title: str, body: str, result: AnalysisResult) -> str:
    footer = (
        f'<p class="disclaimer">{escape(DISCLAIMER)}<br>'
        f"분석 모델: {escape(result.model)} · 토큰 입력 {result.input_tokens:,} / 출력 {result.output_tokens:,}</p>"
    )
    return (
        "<!doctype html>\n<html lang=\"ko\"><head><meta charset=\"utf-8\">"
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        f"<title>{escape(title)}</title><style>{_CSS}</style></head>"
        f"<body><main>{body}{footer}</main></body></html>\n"
    )


# ---------------------------------------------------------------------------------------------
# Markdown
# ---------------------------------------------------------------------------------------------


def _md_list(items: list[str]) -> list[str]:
    return [f"- {i}" for i in items] or ["- 없음"]


def _md_cell(text: str) -> str:
    return text.replace("|", "\\|").replace("\n", " ")


def render_markdown(result: AnalysisResult, image_name: str, created: datetime) -> str:
    a = result.analysis
    lines = [
        f"# {a.title}",
        "",
        " · ".join(_meta_line(a)) + f" · 작성 {created.strftime('%Y-%m-%d %H:%M')}",
        "",
        f"![분석한 차트]({image_name})",
        "",
        "## 핵심 요약",
        "",
        f"**종합 판단: {BIAS_LABEL[a.overall_bias]}** (확신도 {GRADE_LABEL[a.confidence]})",
        "",
        *_md_list(a.summary),
        "",
    ]
    if a.is_chart:
        t = a.trend
        lines += [
            "## 추세",
            "",
            f"- **단기 {SIGNAL_LABEL[t.short_term]}**: {t.short_term_comment}",
            f"- **중기 {SIGNAL_LABEL[t.medium_term]}**: {t.medium_term_comment}",
            f"- **구조**: {t.structure}",
            "",
        ]
        if a.key_levels:
            lines += ["## 주요 가격대", "", "| 구분 | 가격 | 강도 | 근거 |", "|---|---|---|---|"]
            lines += [
                f"| {LEVEL_LABEL[lv.kind]} | {_md_cell(lv.price)} | {GRADE_LABEL[lv.strength]} | {_md_cell(lv.basis)} |"
                for lv in a.key_levels
            ]
            lines.append("")
        if a.patterns:
            lines += ["## 패턴", ""]
            lines += [
                f"- **{p.name}** ({PATTERN_STATUS_LABEL[p.status]}, {SIGNAL_LABEL[p.signal]}): {p.description}"
                for p in a.patterns
            ]
            lines.append("")
        if a.indicators or a.volume_analysis:
            lines += ["## 보조지표 · 거래량", ""]
            lines += [f"- **{i.name}**: {i.reading} ({SIGNAL_LABEL[i.signal]})" for i in a.indicators]
            if a.volume_analysis:
                lines.append(f"- **거래량**: {a.volume_analysis}")
            lines.append("")
        if a.scenarios:
            lines += ["## 시나리오", ""]
            for s in a.scenarios:
                lines += [
                    f"### {SCENARIO_LABEL[s.direction]} (가능성 {GRADE_LABEL[s.likelihood]})",
                    "",
                    s.description,
                    "",
                    f"- 조건: {s.trigger}",
                    f"- 목표: {' → '.join(s.targets) or '-'}",
                    f"- 무효화: {s.invalidation}",
                    "",
                ]
        lines += ["## 주의할 위험", "", *_md_list(a.risks), "", "## 다음에 확인할 것", "", *_md_list(a.watch_points), ""]
    lines += ["## 판독 한계", "", *_md_list(a.limitations), "", "---", "", f"_{DISCLAIMER} 분석 모델: {result.model}_", ""]
    return "\n".join(lines)


# ---------------------------------------------------------------------------------------------
# 저장
# ---------------------------------------------------------------------------------------------


def save_report(
    result: AnalysisResult,
    image: PreparedImage,
    out_dir: Path,
    *,
    source: str,
    created: datetime | None = None,
) -> SavedReport:
    created = created or datetime.now()
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = report_stem(result.analysis, created)
    n = 2
    while (out_dir / f"{stem}.html").exists():
        stem = f"{report_stem(result.analysis, created)}-{n}"
        n += 1

    ext = ".png" if image.media_type == "image/png" else ".jpg"
    image_path = out_dir / f"{stem}{ext}"
    image_path.write_bytes(image.data)

    html_path = out_dir / f"{stem}.html"
    html_path.write_text(render_html(result, image, created, source), encoding="utf-8")

    md_path = out_dir / f"{stem}.md"
    md_path.write_text(render_markdown(result, image_path.name, created), encoding="utf-8")

    json_path = out_dir / f"{stem}.json"
    payload = {
        "created": created.isoformat(timespec="seconds"),
        "source": source,
        "model": result.model,
        "usage": {"input_tokens": result.input_tokens, "output_tokens": result.output_tokens},
        "request_id": result.request_id,
        "image": image_path.name,
        "analysis": result.analysis.model_dump(mode="json"),
    }
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    write_index(out_dir)
    return SavedReport(html=html_path, markdown=md_path, json=json_path, image=image_path)


@dataclass(frozen=True)
class ReportEntry:
    created: str  # "YYYY-MM-DD HH:MM"
    title: str
    instrument: str | None
    timeframe: str | None
    bias: str
    html: Path


def list_reports(out_dir: Path) -> list[ReportEntry]:
    """저장된 보고서를 최신순으로 돌려줍니다. 깨진 JSON 은 건너뜁니다."""
    entries: list[ReportEntry] = []
    if not out_dir.is_dir():
        return entries
    for json_path in sorted(out_dir.glob("*.json"), reverse=True):
        try:
            data = json.loads(json_path.read_text(encoding="utf-8"))
            a = data["analysis"]
            entries.append(
                ReportEntry(
                    created=data["created"].replace("T", " ")[:16],
                    title=a.get("title") or json_path.stem,
                    instrument=a.get("instrument"),
                    timeframe=a.get("timeframe"),
                    bias=a["overall_bias"],
                    html=json_path.with_suffix(".html"),
                )
            )
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            continue
    return entries


def write_index(out_dir: Path) -> Path:
    """저장된 보고서 목록 페이지(index.html)를 최신순으로 다시 만듭니다."""
    rows = [
        "<tr>"
        f"<td class=\"num\">{escape(e.created)}</td>"
        f'<td><a href="{escape(e.html.name)}">{escape(e.title)}</a></td>'
        f"<td>{escape(e.instrument or '-')}</td>"
        f"<td>{escape(e.timeframe or '-')}</td>"
        f"<td>{_badge(BIAS_LABEL.get(e.bias, e.bias), _tone(e.bias))}</td>"
        "</tr>"
        for e in list_reports(out_dir)
    ]
    table = (
        '<div class="table-wrap"><table><thead><tr><th>작성</th><th>제목</th><th>종목</th><th>봉</th><th>판단</th>'
        f"</tr></thead><tbody>{''.join(rows)}</tbody></table></div>"
        if rows
        else '<p class="muted">아직 보고서가 없습니다.</p>'
    )
    page = (
        "<!doctype html>\n<html lang=\"ko\"><head><meta charset=\"utf-8\">"
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        f"<title>차트 분석 보고서 목록</title><style>{_CSS}</style></head>"
        f"<body><main><header><h1>차트 분석 보고서</h1>"
        f'<div class="meta"><span>총 {len(rows)}건</span></div></header>'
        f"<section>{table}</section></main></body></html>\n"
    )
    index_path = out_dir / "index.html"
    index_path.write_text(page, encoding="utf-8")
    return index_path
