from __future__ import annotations

import json
from datetime import datetime

from chart_analyzer.imaging import prepare_image
from chart_analyzer.report import (
    render_html,
    render_markdown,
    report_stem,
    save_report,
    write_index,
)

from .conftest import sample_analysis, sample_result

CREATED = datetime(2026, 10, 2, 14, 30, 15)


def test_html_contains_all_sections(chart_image):
    html = render_html(sample_result(), prepare_image(chart_image), CREATED, "클립보드")
    for heading in ("핵심 요약", "분석한 차트", "추세", "주요 가격대", "패턴", "보조지표 · 거래량", "시나리오", "판독 한계"):
        assert f"<h2>{heading}</h2>" in html
    assert "data:image/png;base64," in html
    assert "64,200" in html and "상승 우위" in html
    assert "투자 조언이 아닙니다" in html


def test_html_escapes_model_text(chart_image):
    result = sample_result(title="<script>alert(1)</script>", summary=['"><img src=x onerror=alert(1)>'])
    html = render_html(result, prepare_image(chart_image), CREATED, "클립보드")
    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;" in html
    assert "<img src=x" not in html


def test_non_chart_image_renders_short_report(chart_image):
    result = sample_result(is_chart=False, summary=["차트가 아닌 사진입니다."], key_levels=[], scenarios=[])
    html = render_html(result, prepare_image(chart_image), CREATED, "클립보드")
    assert "차트가 아닌 사진입니다." in html
    assert "<h2>시나리오</h2>" not in html


def test_markdown_escapes_table_pipes():
    md = render_markdown(sample_result(), "shot.png", CREATED)
    assert md.startswith("# BTCUSDT 4시간봉")
    assert "![분석한 차트](shot.png)" in md
    assert "직전 눌림 저점 \\| EMA50" in md
    assert "### 상승 시나리오 (가능성 보통)" in md


def test_report_stem_is_filename_safe():
    analysis = sample_analysis(instrument="BTC/USDT:PERP", timeframe="4시간")
    assert report_stem(analysis, CREATED) == "20261002-143015_BTC_USDT_PERP_4시간"
    assert report_stem(sample_analysis(instrument=None, timeframe=None), CREATED) == "20261002-143015"


def test_save_report_writes_all_files_and_index(tmp_path, chart_image):
    image = prepare_image(chart_image)
    first = save_report(sample_result(), image, tmp_path, source="클립보드", created=CREATED)
    second = save_report(sample_result(), image, tmp_path, source="클립보드", created=CREATED)

    for path in (first.html, first.markdown, first.json, first.image):
        assert path.is_file()
    assert first.html != second.html  # 같은 시각이어도 덮어쓰지 않음
    assert second.html.name.endswith("-2.html")

    data = json.loads(first.json.read_text(encoding="utf-8"))
    assert data["analysis"]["instrument"] == "BTCUSDT"
    assert data["image"] == first.image.name

    index = (tmp_path / "index.html").read_text(encoding="utf-8")
    assert first.html.name in index and second.html.name in index
    assert "총 2건" in index


def test_index_skips_broken_json(tmp_path):
    (tmp_path / "broken.json").write_text("{not json", encoding="utf-8")
    index = write_index(tmp_path).read_text(encoding="utf-8")
    assert "아직 보고서가 없습니다" in index
