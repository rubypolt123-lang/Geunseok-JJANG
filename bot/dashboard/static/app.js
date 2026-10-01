/* 바이낸스 선물 자동매매 대시보드 — 읽기 전용 (Vanilla JS, 빌드 단계 없음).
 * - 모든 데이터는 GET /api/* 로만 읽습니다. 주문/변경 기능은 없습니다.
 * - 시간은 한국 시간(KST, Asia/Seoul)으로 표시합니다. 차트 JSON 의 time 은 UTC 초입니다.
 * - 지표 이름은 /api/meta 의 metric_labels 를 사용합니다(여기서 하드코딩하지 않음).
 * - 서버 데이터는 textContent 로만 넣습니다(innerHTML 미사용).
 */
'use strict';

(function () {
  // ------------------------------------------------------------------ 한국어 라벨
  const EXIT_REASON_KO = {
    SIGNAL: '신호 청산', FLIP: '포지션 전환', STOP_LOSS: '손절', TAKE_PROFIT: '익절', LIQUIDATION: '강제청산',
    KILL_SWITCH: '킬스위치', END_OF_DATA: '백테스트 종료', PROTECTION_FAILED: '보호주문 실패', MANUAL: '수동 청산',
    UNKNOWN: '알 수 없음',
  };
  const STATE_KO = {
    STARTING: '시작 중', RUNNING: '실행 중', HALTED: '신규 진입 중지', KILL_SWITCH: '킬스위치 발동', ERROR: '오류',
    STOPPED: '정지됨',
  };
  const MODE_KO = { paper: '페이퍼(모의)', testnet: '테스트넷(데모)', live: '실거래' };
  const SIGNAL_KO = { LONG: '롱', SHORT: '숏', CLOSE: '청산', NONE: '없음' };
  const DIRECTION_KO = { LONG: '롱', SHORT: '숏' };
  const KIND_KO = { STOP_LOSS: '손절', TAKE_PROFIT: '익절' };
  const SIDE_KO = { BUY: '매수', SELL: '매도' };
  const BLOCKED_KO = {
    kill_switch: '킬스위치 (일일 손실 한도 도달, UTC 00:00 = KST 09:00 에 해제)',
    cooldown: '쿨다운 (손절/강제청산 후 대기 중)',
    halt_file: 'STOP 파일 존재 (data/STOP 삭제 시 재개)',
  };
  const HALT_REASON_KO = {
    protection_failed: '보호주문(손절) 실패',
    emergency_flatten: '긴급 청산 이후',
    repeated_errors: '반복 오류',
  };

  const TZ = 'Asia/Seoul';
  const BACKTEST_REFRESH_MS = 60000;
  const META_RETRY_MS = 5000;
  const CHART_LIB_FAIL = '차트 라이브러리를 불러오지 못했습니다';
  const CHART_LIB_TIMEOUT_MS = 20000;

  const state = {
    meta: null,
    refreshMs: 10000,
    pricePrecision: 2,
    chartsAvailable: null, // null = 아직 모름, true/false
    candleKey: null,
    equityKey: null,
    selectedRun: null,
    lastCandles: null,
    lastCandlesPayload: null,
    lastEquity: null,
    lastEquityPayload: null,
    lastBtEquity: null,
    lastBtPayload: null,
    equityWallet: new Map(),
  };

  const charts = {
    candle: null, candleSeries: null,
    equity: null, equitySeries: null,
    bt: null, btSeries: null,
  };

  // ------------------------------------------------------------------ DOM 도우미
  const $ = (id) => document.getElementById(id);

  function el(tag, opts, children) {
    const node = document.createElement(tag);
    if (opts) {
      if (opts.className) node.className = opts.className;
      if (opts.text !== undefined && opts.text !== null) node.textContent = String(opts.text);
      if (opts.title) node.title = String(opts.title);
      if (opts.colSpan) node.colSpan = opts.colSpan;
    }
    if (children) {
      for (const child of children) {
        if (child !== null && child !== undefined) node.appendChild(child);
      }
    }
    return node;
  }

  // className 을 주면 클래스를 통째로 바꾸고(빈 문자열 = 모두 제거), 생략하면 기존 클래스를 유지합니다.
  function setText(id, text, className) {
    const node = $(id);
    if (!node) return;
    node.textContent = text === null || text === undefined || text === '' ? '-' : String(text);
    if (className !== undefined) node.className = className;
  }

  function clear(node) {
    while (node.firstChild) node.removeChild(node.firstChild);
  }

  function emptyRow(tbody, colSpan, text) {
    clear(tbody);
    const tr = el('tr', { className: 'empty-row' }, [el('td', { text: text, colSpan: colSpan })]);
    tbody.appendChild(tr);
  }

  function sleep(ms) {
    return new Promise((resolve) => setTimeout(resolve, ms));
  }

  // ------------------------------------------------------------------ 포맷
  const DATE_TIME_OPTS = {
    timeZone: TZ, year: 'numeric', month: '2-digit', day: '2-digit',
    hour: '2-digit', minute: '2-digit', second: '2-digit', hourCycle: 'h23',
  };
  const partsFormatter = new Intl.DateTimeFormat('ko-KR', {
    timeZone: TZ, year: 'numeric', month: 'numeric', day: 'numeric',
    hour: '2-digit', minute: '2-digit', second: '2-digit', hourCycle: 'h23',
  });

  function isNum(v) {
    return v !== null && v !== undefined && v !== '' && Number.isFinite(Number(v));
  }

  function fmtTime(ms) {
    if (!isNum(ms)) return '-';
    return new Date(Number(ms)).toLocaleString('ko-KR', DATE_TIME_OPTS);
  }

  function kstParts(sec) {
    const out = {};
    for (const p of partsFormatter.formatToParts(new Date(Number(sec) * 1000))) out[p.type] = p.value;
    return out;
  }

  function fmtNum(v, digits) {
    if (!isNum(v)) return '-';
    return Number(v).toLocaleString('ko-KR', { minimumFractionDigits: digits, maximumFractionDigits: digits });
  }

  function fmtSigned(v, digits) {
    if (!isNum(v)) return '-';
    const n = Number(v);
    const s = fmtNum(Math.abs(n), digits);
    if (n > 0) return '+' + s;
    if (n < 0) return '−' + s;
    return s;
  }

  function fmtPrice(v) {
    return fmtNum(v, state.pricePrecision);
  }

  function fmtQty(v) {
    if (!isNum(v)) return '-';
    return Number(v).toLocaleString('ko-KR', { minimumFractionDigits: 0, maximumFractionDigits: 8 });
  }

  function fmtPct(fraction) {
    if (!isNum(fraction)) return '-';
    return fmtNum(Number(fraction) * 100, 2) + '%';
  }

  function pnlClass(v) {
    if (!isNum(v)) return '';
    const n = Number(v);
    return n > 0 ? 'pnl-pos' : n < 0 ? 'pnl-neg' : '';
  }

  function fmtAge(sec) {
    if (!isNum(sec)) return '-';
    const s = Math.max(0, Math.round(Number(sec)));
    if (s < 60) return s + '초 전';
    if (s < 3600) return Math.floor(s / 60) + '분 ' + (s % 60) + '초 전';
    if (s < 86400) return Math.floor(s / 3600) + '시간 ' + Math.floor((s % 3600) / 60) + '분 전';
    return Math.floor(s / 86400) + '일 전';
  }

  function decimalsOf(v) {
    if (!isNum(v)) return 0;
    const s = String(Number(Number(v).toPrecision(12)));
    if (s.indexOf('e') >= 0 || s.indexOf('E') >= 0) return 8;
    const dot = s.indexOf('.');
    return dot < 0 ? 0 : s.length - dot - 1;
  }

  // 가격 소수 자릿수: 기본 2자리, 캔들 가격이 더 촘촘하면(예: 0.1234) 그에 맞춤 (최대 8)
  function precisionFromCandles(candles) {
    let d = 2;
    const sample = candles.slice(-200);
    for (const c of sample) {
      d = Math.max(d, decimalsOf(c.open), decimalsOf(c.high), decimalsOf(c.low), decimalsOf(c.close));
    }
    return Math.min(d, 8);
  }

  function modeLabel(mode) {
    return MODE_KO[mode] || (mode ? String(mode) : '-');
  }

  function blockedText(code) {
    if (!code) return '없음';
    if (BLOCKED_KO[code]) return BLOCKED_KO[code];
    if (String(code).startsWith('halted:')) {
      const r = String(code).slice(7);
      return '진입 중지 (' + (HALT_REASON_KO[r] || r) + ') — 재시작 시 해제';
    }
    return String(code);
  }

  function percentMetrics() {
    return new Set((state.meta && state.meta.percent_metrics) || []);
  }

  function metricLabel(key) {
    const labels = (state.meta && state.meta.metric_labels) || {};
    return labels[key] || key;
  }

  function formatMetric(key, v) {
    if (!isNum(v)) return '-';
    const n = Number(v);
    if (percentMetrics().has(key)) return fmtPct(n);
    if (Number.isInteger(n)) return n.toLocaleString('ko-KR');
    return fmtNum(n, Math.abs(n) >= 1 ? 2 : 4);
  }

  // ------------------------------------------------------------------ 네트워크
  async function getJSON(path) {
    const res = await fetch(path, { cache: 'no-store', headers: { Accept: 'application/json' } });
    if (!res.ok) {
      const err = new Error('HTTP ' + res.status + ' ' + path);
      err.status = res.status;
      throw err;
    }
    return res.json();
  }

  function setConnection(ok) {
    const badge = $('conn-badge');
    if (badge) badge.hidden = !!ok;
  }

  function markRefreshed() {
    setText('last-refresh', fmtTime(Date.now()));
  }

  // ------------------------------------------------------------------ 차트 (lightweight-charts v4)
  function cssVar(name) {
    return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  }

  function chartTimeFormatter(time) {
    if (typeof time !== 'number') return String(time);
    return new Date(time * 1000).toLocaleString('ko-KR', DATE_TIME_OPTS) + ' KST';
  }

  function tickMarkFormatter(time, tickMarkType) {
    if (typeof time !== 'number') return null;
    const p = kstParts(time);
    switch (tickMarkType) {
      case 0: return p.year + '년';
      case 1: return p.month + '월';
      case 2: return p.month + '/' + p.day;
      case 4: return p.hour + ':' + p.minute + ':' + p.second;
      default: return p.hour + ':' + p.minute;
    }
  }

  function themeOptions() {
    return {
      layout: {
        background: { type: 'solid', color: cssVar('--surface') },
        textColor: cssVar('--text-secondary'),
        fontFamily: getComputedStyle(document.body).fontFamily,
        fontSize: 12,
      },
      grid: {
        vertLines: { color: cssVar('--grid') },
        horzLines: { color: cssVar('--grid') },
      },
      rightPriceScale: { borderColor: cssVar('--axis') },
      timeScale: { borderColor: cssVar('--axis') },
    };
  }

  function baseChartOptions() {
    const opts = themeOptions();
    opts.autoSize = true;
    opts.timeScale.timeVisible = true;
    opts.timeScale.secondsVisible = false;
    opts.timeScale.tickMarkFormatter = tickMarkFormatter;
    opts.localization = { locale: 'ko-KR', timeFormatter: chartTimeFormatter };
    opts.crosshair = { mode: 0 }; // Normal
    opts.handleScroll = { vertTouchDrag: false };
    return opts;
  }

  function priceFormat(precision) {
    return { type: 'price', precision: precision, minMove: Math.pow(10, -precision) };
  }

  function showChartMessage(id, text) {
    const node = $(id);
    if (!node) return;
    if (text) {
      node.textContent = text;
      node.hidden = false;
    } else {
      node.hidden = true;
    }
  }

  function ascendingUnique(points) {
    const out = [];
    let last = -Infinity;
    for (const p of points) {
      if (!p || typeof p.time !== 'number') continue;
      if (p.time > last) {
        out.push(p);
        last = p.time;
      } else if (p.time === last) {
        out[out.length - 1] = p;
      }
    }
    return out;
  }

  function ensureCandleChart() {
    if (state.chartsAvailable !== true || charts.candle) return !!charts.candle;
    charts.candle = LightweightCharts.createChart($('candle-chart'), baseChartOptions());
    charts.candleSeries = charts.candle.addCandlestickSeries({
      upColor: cssVar('--up'), downColor: cssVar('--down'),
      wickUpColor: cssVar('--up'), wickDownColor: cssVar('--down'),
      borderVisible: false,
      priceFormat: priceFormat(state.pricePrecision),
    });
    charts.candle.subscribeCrosshairMove(updateCandleLegend);
    return true;
  }

  function ensureLineChart(key, containerId, legendFn) {
    if (state.chartsAvailable !== true) return false;
    if (charts[key]) return true;
    const chart = LightweightCharts.createChart($(containerId), baseChartOptions());
    const series = chart.addLineSeries({
      color: cssVar('--accent'), lineWidth: 2,
      priceLineVisible: false, lastValueVisible: true,
      crosshairMarkerRadius: 4,
      priceFormat: priceFormat(2),
    });
    chart.subscribeCrosshairMove(legendFn);
    charts[key] = chart;
    charts[key + 'Series'] = series;
    return true;
  }

  function applyChartTheme() {
    for (const key of ['candle', 'equity', 'bt']) {
      if (charts[key]) charts[key].applyOptions(themeOptions());
    }
    if (charts.candleSeries) {
      charts.candleSeries.applyOptions({
        upColor: cssVar('--up'), downColor: cssVar('--down'),
        wickUpColor: cssVar('--up'), wickDownColor: cssVar('--down'),
      });
    }
    for (const key of ['equitySeries', 'btSeries']) {
      if (charts[key]) charts[key].applyOptions({ color: cssVar('--accent') });
    }
  }

  function legendParts(node, parts) {
    clear(node);
    parts.forEach((part, i) => {
      if (i > 0) node.appendChild(document.createTextNode(' · '));
      if (part.bold) node.appendChild(el('b', { text: part.text }));
      else node.appendChild(document.createTextNode(part.text));
    });
  }

  function updateCandleLegend(param) {
    const node = $('candle-legend');
    if (!node) return;
    let bar = null;
    let time = null;
    if (param && param.time !== undefined && param.seriesData && charts.candleSeries) {
      bar = param.seriesData.get(charts.candleSeries) || null;
      time = param.time;
    }
    if (!bar && state.lastCandles && state.lastCandles.length) {
      bar = state.lastCandles[state.lastCandles.length - 1];
      time = bar.time;
    }
    if (!bar) {
      clear(node);
      return;
    }
    legendParts(node, [
      { text: chartTimeFormatter(time) },
      { text: '시 ' + fmtPrice(bar.open) },
      { text: '고 ' + fmtPrice(bar.high) },
      { text: '저 ' + fmtPrice(bar.low) },
      { text: '종 ' + fmtPrice(bar.close), bold: true },
    ]);
  }

  function lineLegend(nodeId, seriesKey, lastKey, withWallet) {
    return function (param) {
      const node = $(nodeId);
      if (!node) return;
      let point = null;
      let time = null;
      if (param && param.time !== undefined && param.seriesData && charts[seriesKey]) {
        point = param.seriesData.get(charts[seriesKey]) || null;
        time = param.time;
      }
      const last = state[lastKey];
      if (!point && last && last.length) {
        point = last[last.length - 1];
        time = point.time;
      }
      if (!point) {
        clear(node);
        return;
      }
      const parts = [{ text: chartTimeFormatter(time) }, { text: '평가 자산 ' + fmtNum(point.value, 2) + ' USDT', bold: true }];
      if (withWallet && state.equityWallet.has(time)) {
        parts.push({ text: '지갑 ' + fmtNum(state.equityWallet.get(time), 2) });
      }
      legendParts(node, parts);
    };
  }

  const updateEquityLegend = lineLegend('equity-legend', 'equitySeries', 'lastEquity', true);
  const updateBtLegend = lineLegend('bt-equity-legend', 'btSeries', 'lastBtEquity', false);

  function chartsUnavailableMessage() {
    for (const id of ['candle-message', 'equity-message', 'bt-equity-message']) showChartMessage(id, CHART_LIB_FAIL);
  }

  function watchChartLibrary() {
    return new Promise((resolve) => {
      let settled = false;
      const finish = (ok) => {
        if (settled) return;
        settled = true;
        resolve(ok);
      };
      if (window.LightweightCharts) {
        finish(true);
        return;
      }
      const script = $('lwc-script');
      if (script) {
        script.addEventListener('load', () => finish(!!window.LightweightCharts));
        script.addEventListener('error', () => finish(false));
      }
      // async 스크립트가 끝나야 window load 가 발생하므로, 이미 끝났다면 결과를 바로 확인
      if (document.readyState === 'complete') finish(!!window.LightweightCharts);
      else window.addEventListener('load', () => finish(!!window.LightweightCharts), { once: true });
      setTimeout(() => finish(!!window.LightweightCharts), CHART_LIB_TIMEOUT_MS);
    });
  }

  function onChartLibraryResult(ok) {
    state.chartsAvailable = !!ok;
    if (!ok) {
      chartsUnavailableMessage();
      // 늦게라도 로드되면 차트를 켭니다
      const script = $('lwc-script');
      if (script) {
        script.addEventListener('load', () => {
          if (window.LightweightCharts && state.chartsAvailable !== true) {
            onChartLibraryResult(true);
          }
        }, { once: true });
      }
      return;
    }
    for (const id of ['candle-message', 'equity-message', 'bt-equity-message']) showChartMessage(id, '');
    if (state.lastCandles) renderCandles(state.lastCandlesPayload);
    if (state.lastEquityPayload) renderEquity(state.lastEquityPayload);
    if (state.lastBtPayload) renderBacktestEquity(state.lastBtPayload);
  }

  // ------------------------------------------------------------------ 렌더링: 헤더/상태
  function renderModeBadge(mode) {
    const badge = $('mode-badge');
    if (!badge) return;
    badge.textContent = '모드: ' + modeLabel(mode);
    badge.className = 'badge badge-mode mode-' + (mode || 'unknown');
    badge.title = mode === 'live' ? '실거래 모드: 실제 자금이 사용됩니다' : '';
  }

  function renderStatus(payload) {
    const s = payload ? payload.status : null;
    const empty = $('status-empty');
    const stateBadge = $('state-badge');
    if (!s) {
      if (empty) empty.hidden = false;
      for (const id of ['st-mode', 'st-symbol', 'st-interval', 'st-strategy', 'st-state', 'st-heartbeat', 'st-wallet',
        'st-equity', 'st-available', 'st-upnl', 'st-signal', 'st-blocked', 'st-message']) {
        setText(id, '-', $(id) && $(id).classList.contains('num') ? 'num' : '');
      }
      if (stateBadge) {
        stateBadge.textContent = '기록 없음';
        stateBadge.className = 'badge';
      }
      renderModeBadge(state.meta ? state.meta.mode : null);
      renderPosition(null);
      renderProtective([]);
      return;
    }
    if (empty) empty.hidden = true;
    renderModeBadge(s.mode);
    setText('st-mode', modeLabel(s.mode));
    setText('st-symbol', s.symbol);
    setText('st-interval', s.interval);
    setText('st-strategy', s.strategy);
    const stateText = STATE_KO[s.state] || s.state;
    setText('st-state', stateText);
    if (stateBadge) {
      stateBadge.textContent = s.stale && s.state !== 'STOPPED' ? '응답 없음' : stateText;
      stateBadge.className = 'badge ' + (s.stale && s.state !== 'STOPPED' ? 'stale' : 'state-' + s.state);
    }
    if (s.stale) setText('st-heartbeat', '응답 없음 (' + fmtAge(s.heartbeat_age_sec) + ')', 'text-danger');
    else setText('st-heartbeat', fmtAge(s.heartbeat_age_sec), '');

    const acc = s.account || null;
    setText('st-wallet', acc ? fmtNum(acc.wallet_balance, 2) + ' USDT' : '-', 'num');
    setText('st-equity', acc ? fmtNum(acc.equity, 2) + ' USDT' : '-', 'num');
    setText('st-available', acc ? fmtNum(acc.available_balance, 2) + ' USDT' : '-', 'num');
    setText('st-upnl', acc ? fmtSigned(acc.unrealized_pnl, 2) + ' USDT' : '-', 'num ' + (acc ? pnlClass(acc.unrealized_pnl) : ''));

    const sig = s.last_signal;
    if (sig && sig.action) {
      const parts = [
        SIGNAL_KO[sig.action] || sig.action,
        '봉 ' + fmtTime(sig.bar_open_time),
        '가격 ' + fmtPrice(sig.price),
      ];
      setText('st-signal', parts.join(' · ') + (sig.reason ? ' (' + sig.reason + ')' : ''));
    } else {
      setText('st-signal', '-');
    }
    setText('st-blocked', blockedText(s.entries_blocked_reason), s.entries_blocked_reason ? 'text-danger' : '');
    setText('st-message', s.message || '-');

    renderPosition(s.position || null);
    renderProtective(s.protective_orders || []);
  }

  function renderPosition(pos) {
    const empty = $('position-empty');
    const grid = $('position-grid');
    if (!pos || !isNum(pos.qty) || Number(pos.qty) === 0) {
      if (empty) empty.hidden = false;
      if (grid) grid.hidden = true;
      return;
    }
    if (empty) empty.hidden = true;
    if (grid) grid.hidden = false;
    const qty = Number(pos.qty);
    const dir = qty > 0 ? 'LONG' : 'SHORT';
    setText('pos-direction', DIRECTION_KO[dir], dir === 'LONG' ? 'dir-long' : 'dir-short');
    setText('pos-qty', fmtQty(Math.abs(qty)), 'num');
    setText('pos-entry', fmtPrice(pos.entry_price), 'num');
    setText('pos-mark', fmtPrice(pos.mark_price), 'num');
    setText('pos-upnl', fmtSigned(pos.unrealized_pnl, 2) + ' USDT', 'num ' + pnlClass(pos.unrealized_pnl));
    setText('pos-liq', fmtPrice(pos.liquidation_price), 'num');
    setText('pos-leverage', isNum(pos.leverage) ? pos.leverage + '배' : '-', 'num');
    setText('pos-margin', isNum(pos.isolated_margin) ? fmtNum(pos.isolated_margin, 2) + ' USDT' : '-', 'num');
  }

  function renderProtective(orders) {
    const tbody = $('protective-body');
    if (!tbody) return;
    if (!orders || !orders.length) {
      emptyRow(tbody, 5, '보호 주문 없음');
      return;
    }
    clear(tbody);
    for (const o of orders) {
      const tr = el('tr', null, [
        el('td', { text: KIND_KO[o.kind] || o.kind || '-' }),
        el('td', { text: SIDE_KO[o.side] || o.side || '-' }),
        el('td', { className: 'num', text: fmtPrice(o.trigger_price) }),
        el('td', { text: o.status || '-' }),
        el('td', {
          className: 'mono',
          text: o.client_id || '-',
          title: o.exchange_id ? '거래소 ID: ' + o.exchange_id : '',
        }),
      ]);
      tbody.appendChild(tr);
    }
  }

  // ------------------------------------------------------------------ 렌더링: 거래/이벤트
  function renderTradesTable(tbody, trades, emptyText) {
    if (!tbody) return;
    if (!trades || !trades.length) {
      emptyRow(tbody, 11, emptyText);
      return;
    }
    clear(tbody);
    const frag = document.createDocumentFragment();
    for (const t of trades) {
      const dir = t.direction;
      frag.appendChild(el('tr', null, [
        el('td', { text: fmtTime(t.entry_time) }),
        el('td', { text: fmtTime(t.exit_time) }),
        el('td', { className: dir === 'LONG' ? 'dir-long' : dir === 'SHORT' ? 'dir-short' : '', text: DIRECTION_KO[dir] || dir }),
        el('td', { className: 'num', text: fmtQty(t.qty) }),
        el('td', { className: 'num', text: fmtPrice(t.entry_price) }),
        el('td', { className: 'num', text: fmtPrice(t.exit_price) }),
        el('td', { text: EXIT_REASON_KO[t.exit_reason] || t.exit_reason || '-' }),
        el('td', { className: 'num', text: fmtNum(t.fees, 2) }),
        el('td', { className: 'num', text: fmtSigned(t.funding, 2) }),
        el('td', { className: 'num ' + pnlClass(t.net_pnl), text: fmtSigned(t.net_pnl, 2) }),
        el('td', { className: 'num ' + pnlClass(t.r_multiple), text: isNum(t.r_multiple) ? fmtSigned(t.r_multiple, 2) : '-' }),
      ]));
    }
    tbody.appendChild(frag);
  }

  function renderTrades(payload, mode) {
    setText('trades-subtitle', mode ? modeLabel(mode) + ' · 최근 200건 (펀딩비 + = 지불)' : '', 'muted');
    renderTradesTable($('trades-body'), payload ? payload.trades : [], '거래 내역 없음');
  }

  function renderEvents(payload) {
    const list = $('events-list');
    if (!list) return;
    const events = payload ? payload.events : [];
    clear(list);
    if (!events || !events.length) {
      list.appendChild(el('li', { className: 'empty', text: '이벤트 없음' }));
      return;
    }
    for (const e of events) {
      const level = String(e.level || 'INFO').toUpperCase();
      list.appendChild(el('li', null, [
        el('span', { className: 'event-time', text: fmtTime(e.ts) }),
        el('span', null, [
          el('span', { className: 'level level-' + level, text: level }),
          document.createTextNode(' '),
          el('span', { className: 'event-kind', text: e.kind || '-' }),
          el('span', { className: 'muted', text: ' · ' + modeLabel(e.mode) }),
        ]),
        el('span', { className: 'event-msg', text: e.message || '' }),
      ]));
    }
  }

  // ------------------------------------------------------------------ 렌더링: 차트 데이터
  function renderCandles(payload) {
    state.lastCandlesPayload = payload;
    const candles = ascendingUnique((payload && payload.candles) || []);
    state.lastCandles = candles;
    if (candles.length) state.pricePrecision = precisionFromCandles(candles);
    if (payload) setText('candle-subtitle', (payload.symbol || '') + ' · ' + (payload.interval || ''), 'muted');
    if (state.chartsAvailable === false) return;
    if (state.chartsAvailable !== true) return; // 라이브러리 로딩 대기 중
    if (!candles.length) {
      showChartMessage('candle-message', '캔들 데이터 없음 — 트레이더(trade)가 실행되면 표시됩니다');
      return;
    }
    showChartMessage('candle-message', '');
    try {
      ensureCandleChart();
      charts.candleSeries.applyOptions({ priceFormat: priceFormat(state.pricePrecision) });
      charts.candleSeries.setData(candles);
      const markers = ((payload && payload.markers) || []).filter((m) => typeof m.time === 'number');
      markers.sort((a, b) => a.time - b.time);
      charts.candleSeries.setMarkers(markers);
      const key = (payload.symbol || '') + '|' + (payload.interval || '');
      if (state.candleKey !== key) {
        charts.candle.timeScale().fitContent();
        state.candleKey = key;
      }
      updateCandleLegend(null);
    } catch (err) {
      console.error('candle chart update failed', err);
    }
  }

  function renderEquity(payload) {
    state.lastEquityPayload = payload;
    const raw = ascendingUnique((payload && payload.points) || []);
    state.equityWallet = new Map(raw.map((p) => [p.time, p.wallet]));
    const data = raw.map((p) => ({ time: p.time, value: Number(p.equity) }));
    state.lastEquity = data;
    if (payload) setText('equity-subtitle', modeLabel(payload.mode) + ' · 봉 마감 기준 (USDT)', 'muted');
    if (state.chartsAvailable !== true) return;
    if (!data.length) {
      showChartMessage('equity-message', '자산 기록 없음 — 트레이더(trade)가 실행되면 표시됩니다');
      return;
    }
    showChartMessage('equity-message', '');
    try {
      ensureLineChart('equity', 'equity-chart', updateEquityLegend);
      charts.equitySeries.setData(data);
      const key = String(payload.mode || '');
      if (state.equityKey !== key) {
        charts.equity.timeScale().fitContent();
        state.equityKey = key;
      }
      updateEquityLegend(null);
    } catch (err) {
      console.error('equity chart update failed', err);
    }
  }

  function renderBacktestEquity(payload) {
    state.lastBtPayload = payload;
    const data = ascendingUnique((payload && payload.equity) || []).map((p) => ({ time: p.time, value: Number(p.equity) }));
    state.lastBtEquity = data;
    if (state.chartsAvailable !== true) return;
    if (!data.length) {
      showChartMessage('bt-equity-message', '자산 곡선 데이터 없음');
      return;
    }
    showChartMessage('bt-equity-message', '');
    try {
      ensureLineChart('bt', 'bt-equity-chart', updateBtLegend);
      charts.btSeries.setData(data);
      charts.bt.timeScale().fitContent();
      updateBtLegend(null);
    } catch (err) {
      console.error('backtest chart update failed', err);
    }
  }

  // ------------------------------------------------------------------ 백테스트
  function renderBacktests(payload) {
    const tbody = $('backtests-body');
    if (!tbody) return;
    const runs = (payload && payload.runs) || [];
    if (!runs.length) {
      clear(tbody);
      const td = el('td', { colSpan: 10 }, [
        document.createTextNode('백테스트 결과 없음. '),
        el('code', { text: 'python -m bot backtest' }),
        document.createTextNode(' 로 실행하세요.'),
      ]);
      tbody.appendChild(el('tr', { className: 'empty-row' }, [td]));
      return;
    }
    clear(tbody);
    for (const r of runs) {
      const m = r.metrics || {};
      const tr = el('tr', null, [
        el('td', { className: 'mono', text: r.run_id }),
        el('td', { text: fmtTime(r.created_at) }),
        el('td', { text: r.symbol }),
        el('td', { text: r.interval }),
        el('td', { text: r.strategy }),
        el('td', { className: 'num ' + pnlClass(m.total_return), text: formatMetric('total_return', m.total_return) }),
        el('td', { className: 'num', text: formatMetric('max_drawdown', m.max_drawdown) }),
        el('td', { className: 'num', text: formatMetric('sharpe', m.sharpe) }),
        el('td', { className: 'num', text: formatMetric('n_trades', m.n_trades) }),
        el('td', { className: 'num', text: formatMetric('win_rate', m.win_rate) }),
      ]);
      tr.tabIndex = 0;
      tr.dataset.runId = r.run_id;
      if (r.run_id === state.selectedRun) tr.classList.add('selected');
      tr.addEventListener('click', () => selectRun(r.run_id));
      tr.addEventListener('keydown', (ev) => {
        if (ev.key === 'Enter' || ev.key === ' ') {
          ev.preventDefault();
          selectRun(r.run_id);
        }
      });
      tbody.appendChild(tr);
    }
  }

  function markSelectedRow() {
    const tbody = $('backtests-body');
    if (!tbody) return;
    for (const tr of tbody.querySelectorAll('tr')) {
      tr.classList.toggle('selected', !!state.selectedRun && tr.dataset.runId === state.selectedRun);
    }
  }

  function paramsText(params) {
    if (!params || typeof params !== 'object') return '';
    return Object.keys(params).map((k) => k + '=' + params[k]).join(', ');
  }

  function renderMetricsGrid(metrics) {
    const grid = $('bt-metrics');
    if (!grid) return;
    clear(grid);
    const labels = (state.meta && state.meta.metric_labels) || {};
    const keys = Object.keys(labels).filter((k) => Object.prototype.hasOwnProperty.call(metrics, k));
    for (const k of Object.keys(metrics)) {
      if (!Object.prototype.hasOwnProperty.call(labels, k)) keys.push(k);
    }
    for (const k of keys) {
      const cls = k === 'total_return' || k === 'cagr' || k === 'expectancy' || k === 'expectancy_r' ? pnlClass(metrics[k]) : '';
      grid.appendChild(el('div', null, [
        el('dt', { text: metricLabel(k) }),
        el('dd', { className: cls, text: formatMetric(k, metrics[k]) }),
      ]));
    }
  }

  async function selectRun(runId) {
    state.selectedRun = runId;
    markSelectedRow();
    const detail = $('bt-detail');
    if (detail) detail.hidden = false;
    setText('bt-detail-title', '백테스트 상세 — ' + runId, '');
    setText('bt-detail-info', '불러오는 중…', 'muted');
    try {
      const payload = await getJSON('/api/backtests/' + encodeURIComponent(runId));
      if (state.selectedRun !== runId) return;
      setConnection(true);
      const run = payload.run || {};
      const info = [
        (run.symbol || '') + ' ' + (run.interval || ''),
        (run.strategy || '') + (paramsText(run.params) ? ' (' + paramsText(run.params) + ')' : ''),
        '기간 ' + fmtTime(run.start_time) + ' ~ ' + fmtTime(run.end_time),
        metricLabel('initial_balance') + ' ' + fmtNum(run.initial_balance, 2) + ' USDT',
      ];
      setText('bt-detail-info', info.join(' · '), 'muted');
      renderMetricsGrid(run.metrics || {});
      renderBacktestEquity(payload);
      renderTradesTable($('bt-trades-body'), payload.trades || [], '거래 없음');
    } catch (err) {
      if (state.selectedRun !== runId) return;
      if (err && err.status === 404) {
        setText('bt-detail-info', '백테스트를 찾을 수 없습니다: ' + runId, 'text-danger');
      } else {
        setConnection(false);
        setText('bt-detail-info', '서버 연결 실패 — 잠시 후 다시 클릭하세요', 'text-danger');
      }
    }
  }

  function closeDetail() {
    state.selectedRun = null;
    const detail = $('bt-detail');
    if (detail) detail.hidden = true;
    markSelectedRow();
  }

  // ------------------------------------------------------------------ 새로고침 루프
  async function refreshLive() {
    const results = await Promise.allSettled([
      getJSON('/api/status'),
      getJSON('/api/trades?limit=200'),
      getJSON('/api/equity'),
      getJSON('/api/candles?limit=300'),
      getJSON('/api/events?limit=50'),
    ]);
    let failed = false;
    const value = (i) => {
      if (results[i].status === 'fulfilled') return results[i].value;
      failed = true;
      return undefined;
    };
    const status = value(0);
    const trades = value(1);
    const equity = value(2);
    const candles = value(3);
    const events = value(4);
    const safe = (fn) => {
      try {
        fn();
      } catch (err) {
        console.error(err);
      }
    };
    // 캔들을 먼저 처리해 가격 소수 자릿수를 정한 뒤 나머지를 그립니다
    if (candles !== undefined) safe(() => renderCandles(candles));
    if (status !== undefined) safe(() => renderStatus(status));
    const mode = status && status.status ? status.status.mode : state.meta ? state.meta.mode : null;
    if (trades !== undefined) safe(() => renderTrades(trades, mode));
    if (equity !== undefined) safe(() => renderEquity(equity));
    if (events !== undefined) safe(() => renderEvents(events));
    setConnection(!failed);
    if (!failed) markRefreshed();
  }

  async function refreshBacktests() {
    try {
      renderBacktests(await getJSON('/api/backtests?limit=50'));
    } catch (err) {
      setConnection(false);
    }
  }

  function loopLive() {
    refreshLive()
      .catch((err) => {
        console.error(err);
        setConnection(false);
      })
      .finally(() => setTimeout(loopLive, state.refreshMs));
  }

  function loopBacktests() {
    refreshBacktests().finally(() => setTimeout(loopBacktests, BACKTEST_REFRESH_MS));
  }

  async function loadMeta() {
    for (;;) {
      try {
        state.meta = await getJSON('/api/meta');
        setConnection(true);
        return;
      } catch (err) {
        setConnection(false);
        await sleep(META_RETRY_MS);
      }
    }
  }

  async function init() {
    const closeBtn = $('bt-detail-close');
    if (closeBtn) closeBtn.addEventListener('click', closeDetail);

    watchChartLibrary().then(onChartLibraryResult);
    if (window.matchMedia) {
      const mq = window.matchMedia('(prefers-color-scheme: dark)');
      const onChange = () => applyChartTheme();
      if (mq.addEventListener) mq.addEventListener('change', onChange);
      else if (mq.addListener) mq.addListener(onChange);
    }

    await loadMeta();
    const refreshSec = Number(state.meta.refresh_sec);
    state.refreshMs = Math.max(2, Number.isFinite(refreshSec) ? refreshSec : 10) * 1000;
    renderModeBadge(state.meta.mode);
    setText('app-version', 'v' + (state.meta.version || ''), '');
    loopLive();
    loopBacktests();
  }

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init);
  else init();
})();
