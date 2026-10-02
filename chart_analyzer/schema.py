"""차트 분석 결과의 구조(스키마).

Claude 에게 이 구조 그대로 JSON 으로 답하도록 요구합니다(structured outputs).
필드 설명(description)도 모델에게 전달되므로, 무엇을 채워야 하는지 여기서 정의합니다.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

Signal = Literal["bullish", "bearish", "neutral"]
Bias = Literal["strong_bullish", "bullish", "neutral", "bearish", "strong_bearish"]
Grade = Literal["high", "medium", "low"]


class TrendView(BaseModel):
    short_term: Signal = Field(description="화면 오른쪽 최근 구간(최근 20~30개 봉)의 추세 방향")
    short_term_comment: str = Field(description="단기 추세 판단 근거 1~2문장")
    medium_term: Signal = Field(description="화면 전체 구간의 추세 방향")
    medium_term_comment: str = Field(description="중기 추세 판단 근거 1~2문장")
    structure: str = Field(description="고점·저점 구조(고점 높이기/낮추기 등)와 추세선·채널 설명")


class PriceLevel(BaseModel):
    kind: Literal["support", "resistance"]
    price: str = Field(description="차트 가격축에서 읽은 가격 또는 가격대. 예: '64,200' 또는 '63,800~64,100'")
    strength: Grade = Field(description="반응 횟수·거래량·시간 프레임을 고려한 강도")
    basis: str = Field(description="이 가격대를 고른 근거(몇 번 반응했는지, 어떤 구조인지)")


class PatternFinding(BaseModel):
    name: str = Field(description="패턴 이름. 예: 상승 삼각형, 헤드앤숄더, 이중 바닥, 상승 장악형 캔들")
    status: Literal["forming", "confirmed", "failed"]
    signal: Signal
    description: str = Field(description="패턴 위치와 의미, 완성(돌파) 조건")


class IndicatorReading(BaseModel):
    name: str = Field(description="화면에 실제로 보이는 지표 이름. 예: RSI(14), MACD, EMA 20/50, 볼린저밴드")
    reading: str = Field(description="읽은 값 또는 상태. 예: 'RSI 68, 과매수 직전', 'EMA20 이 EMA50 위'")
    signal: Signal


class Scenario(BaseModel):
    direction: Literal["bullish", "bearish", "sideways"]
    likelihood: Grade = Field(description="다른 시나리오와 비교한 상대적 가능성")
    trigger: str = Field(description="이 시나리오가 시작되었다고 볼 조건. 예: '64,200 위 4시간봉 종가 마감'")
    targets: list[str] = Field(description="목표 가격대(가까운 것부터)")
    invalidation: str = Field(description="이 시나리오가 틀렸다고 판단할 가격·조건")
    description: str = Field(description="전개 과정 설명 2~3문장")


class ChartAnalysis(BaseModel):
    is_chart: bool = Field(description="이미지가 가격 차트이면 true. 차트가 아니면 false 로 두고 summary 에 이유를 적습니다")
    title: str = Field(description="보고서 제목. 예: 'BTCUSDT 4시간봉 — 저항 돌파 시도'")
    instrument: str | None = Field(description="종목·심볼. 화면에서 읽을 수 없으면 null")
    timeframe: str | None = Field(description="봉 간격(1분, 15분, 1시간, 4시간, 일봉 등). 읽을 수 없으면 null")
    chart_type: str = Field(description="차트 종류. 예: 캔들, 하이킨아시, 라인, 바")
    current_price: str | None = Field(description="마지막 봉 기준 현재가. 읽을 수 없으면 null")
    summary: list[str] = Field(description="핵심 요약 3줄. 각 줄은 한 문장")
    overall_bias: Bias = Field(description="종합 방향성 판단")
    confidence: Grade = Field(description="판단 확신도. 신호가 엇갈리거나 화면 정보가 부족하면 low")
    trend: TrendView
    key_levels: list[PriceLevel] = Field(description="중요한 지지·저항 가격대. 현재가와 가까운 순서로 최대 6개")
    patterns: list[PatternFinding] = Field(description="보이는 차트·캔들 패턴. 없으면 빈 배열")
    indicators: list[IndicatorReading] = Field(description="화면에 표시된 보조지표 판독. 표시된 지표가 없으면 빈 배열")
    volume_analysis: str | None = Field(description="거래량 해석. 거래량이 화면에 없으면 null")
    scenarios: list[Scenario] = Field(description="앞으로의 시나리오 2~3개(상승/하락/횡보), 가능성 높은 순")
    risks: list[str] = Field(description="주의할 위험 요소")
    watch_points: list[str] = Field(description="다음 봉들에서 확인할 체크포인트")
    limitations: list[str] = Field(description="이미지만으로 판단한 한계, 읽기 어려웠던 부분")
