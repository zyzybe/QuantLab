/* QuantLab 前端：搜索联想 → 动态回测 → 净值曲线与四项指标 */
'use strict';

const $ = (id) => document.getElementById(id);
const esc = (s) => String(s ?? '').replace(/[&<>"']/g,
  (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

const COLORS = { strategy: '#2563eb', benchmark: '#98a2b3', buy_hold: '#f79009' };
const SERIES_LABEL = { strategy: '策略净值', benchmark: '基准净值', buy_hold: '买入持有' };
const CORE = [
  { key: 'cumulative_return', label: '累计收益率', pct: true, tag: '必需指标' },
  { key: 'annualized_return', label: '年化收益率', pct: true, tag: '必需指标' },
  { key: 'max_drawdown', label: '最大回撤', pct: true, tag: '越小越好' },
  { key: 'sharpe_ratio', label: '夏普比率', pct: false, tag: '必需指标' },
];

const S = {
  config: null, report: null, selected: null,
  visible: { strategy: true, benchmark: true, buy_hold: true },
  chart: null, timers: [], suggestItems: [], activeIndex: -1,
  debounce: null, resizeDebounce: null, running: false,
};

async function fetchJSON(url, options) {
  const res = await fetch(url, options);
  if (!res.ok && res.status !== 400) throw new Error(`HTTP ${res.status}`);
  return res.json();
}

function fmt(v, pct, signed = true) {
  if (v === null || v === undefined || !Number.isFinite(Number(v))) return '—';
  const n = Number(v);
  const sign = signed && n > 0 ? '+' : '';
  return pct ? `${sign}${(n * 100).toFixed(2)}%` : `${sign}${n.toFixed(2)}`;
}

function cls(v) {
  if (v === null || v === undefined || !Number.isFinite(Number(v))) return 'flat';
  return Number(v) > 0 ? 'up' : (Number(v) < 0 ? 'down' : 'flat');
}

function fmtMoney(v) {
  return Math.abs(v) >= 10000 ? `${(v / 10000).toFixed(1)}万` : v.toFixed(0);
}

function showMessage(kind, html) {
  const box = $('message');
  box.className = `message ${kind}`;
  box.innerHTML = html;
  box.classList.remove('hidden');
}

function clearMessage() {
  $('message').classList.add('hidden');
  $('message').innerHTML = '';
}

/* ------------------------------------------------------------ 初始化 */
async function init() {
  try {
    const [cfg, health] = await Promise.all([
      fetchJSON('/api/config'), fetchJSON('/api/health'),
    ]);
    S.config = cfg;
    $('startDate').value = cfg.default_start;
    $('endDate').value = cfg.default_end;
    const sel = $('benchmark');
    sel.innerHTML = cfg.benchmarks
      .map((b) => `<option value="${esc(b.code)}">${esc(b.name)} (${esc(b.code)})</option>`).join('');
    sel.value = cfg.benchmark;
    const p = cfg.parameters;
    const map = {
      initialCapital: 'initial_capital', fastWindow: 'fast_window', slowWindow: 'slow_window',
      maxPosition: 'max_position', stopLoss: 'stop_loss', cooldownDays: 'cooldown_days',
      voteThreshold: 'vote_threshold', rf: 'rf',
    };
    Object.entries(map).forEach(([id, key]) => { $(id).value = p[key]; });
    $('symbolInput').value = cfg.default_symbol;
    $('healthDot').className = 'status-dot ok';
    $('healthText').textContent = `后端已连接 · 名录 ${health.universe_count} 只`;
    $('version').textContent = `v${health.version}`;
  } catch (err) {
    $('healthDot').className = 'status-dot bad';
    $('healthText').textContent = `后端连接失败：${err.message}`;
  }
  bindEvents();
  renderLegend();
}

function bindEvents() {
  const input = $('symbolInput');
  input.addEventListener('input', () => {
    S.selected = null;
    $('selectedName').textContent = '';
    clearTimeout(S.debounce);
    const q = input.value.trim();
    if (!q) { hideSuggestions(); return; }
    S.debounce = setTimeout(() => loadSuggestions(q), 220);
  });
  input.addEventListener('keydown', (e) => {
    if ($('suggestions').classList.contains('hidden')) {
      if (e.key === 'Enter') { e.preventDefault(); runBacktest(); }
      return;
    }
    if (e.key === 'ArrowDown') { e.preventDefault(); S.activeIndex = Math.min(S.activeIndex + 1, S.suggestItems.length - 1); paintActive(); }
    else if (e.key === 'ArrowUp') { e.preventDefault(); S.activeIndex = Math.max(S.activeIndex - 1, 0); paintActive(); }
    else if (e.key === 'Enter') { e.preventDefault(); if (S.activeIndex >= 0) pickSymbol(S.suggestItems[S.activeIndex]); else { hideSuggestions(); runBacktest(); } }
    else if (e.key === 'Escape') hideSuggestions();
  });
  document.addEventListener('click', (e) => {
    if (!e.target.closest('.search-box')) hideSuggestions();
  });
  document.querySelectorAll('.chip').forEach((chip) => {
    chip.addEventListener('click', () => {
      input.value = chip.dataset.code;
      S.selected = null;
      $('selectedName').textContent = chip.textContent;
      hideSuggestions();
      runBacktest();
    });
  });
  $('runBtn').addEventListener('click', runBacktest);
  $('reRunBtn').addEventListener('click', runBacktest);
  window.addEventListener('resize', () => {
    clearTimeout(S.resizeDebounce);
    S.resizeDebounce = setTimeout(() => { if (S.report) renderChart(S.report); }, 180);
  });
}

/* ------------------------------------------------------------ 搜索联想 */
async function loadSuggestions(q) {
  try {
    const r = await fetchJSON(`/api/universe?q=${encodeURIComponent(q)}`);
    S.suggestItems = r.results || [];
    S.activeIndex = S.suggestItems.length ? 0 : -1;
    const box = $('suggestions');
    if (!S.suggestItems.length) {
      box.innerHTML = '<div class="suggest-empty">未匹配到标的；可直接输入 6 位股票代码查询</div>';
    } else {
      box.innerHTML = S.suggestItems.map((it, i) => `
        <div class="suggest-item${i === S.activeIndex ? ' active' : ''}" data-index="${i}">
          <span class="code">${esc(it.code)}</span>
          <span class="name">${esc(it.name)}</span>
          <span class="market">${esc(it.market || '')}</span>
        </div>`).join('');
      box.querySelectorAll('.suggest-item').forEach((el) => {
        el.addEventListener('click', () => pickSymbol(S.suggestItems[Number(el.dataset.index)]));
      });
    }
    box.classList.remove('hidden');
  } catch (err) {
    $('suggestions').innerHTML = `<div class="suggest-empty">名录查询失败：${esc(err.message)}</div>`;
    $('suggestions').classList.remove('hidden');
  }
}

function paintActive() {
  $('suggestions').querySelectorAll('.suggest-item').forEach((el, i) => {
    el.classList.toggle('active', i === S.activeIndex);
  });
}

function pickSymbol(item) {
  $('symbolInput').value = item.code;
  $('selectedName').textContent = item.unlisted ? '代码直查' : item.name;
  S.selected = item;
  hideSuggestions();
}

function hideSuggestions() { $('suggestions').classList.add('hidden'); }

/* ------------------------------------------------------------ 四模块动画 */
function stages() { return Array.from(document.querySelectorAll('.stage')); }

function clearTimers() { S.timers.forEach(clearTimeout); S.timers = []; }

function animateStages() {
  clearTimers();
  const list = stages();
  list.forEach((s) => {
    s.classList.remove('running', 'done', 'error');
    s.querySelector('.stage-state').textContent = '待运行';
  });
  let i = 0;
  const tick = () => {
    if (i > 0) {
      list[i - 1].classList.remove('running');
      list[i - 1].classList.add('done');
      list[i - 1].querySelector('.stage-state').textContent = '完成';
    }
    if (i < list.length) {
      list[i].classList.add('running');
      list[i].querySelector('.stage-state').textContent = '运行中';
      i += 1;
      S.timers.push(setTimeout(tick, 430));
    }
  };
  tick();
}

function finishStages(ok) {
  clearTimers();
  stages().forEach((s) => {
    s.classList.remove('running', 'done', 'error');
    s.classList.add(ok ? 'done' : 'error');
    s.querySelector('.stage-state').textContent = ok ? '完成' : '未产出';
  });
}

/* ------------------------------------------------------------ 运行回测 */
function collectPayload() {
  return {
    symbol: $('symbolInput').value.trim(),
    start_date: $('startDate').value,
    end_date: $('endDate').value,
    benchmark: $('benchmark').value,
    initial_capital: Number($('initialCapital').value),
    fast_window: Number($('fastWindow').value),
    slow_window: Number($('slowWindow').value),
    max_position: Number($('maxPosition').value),
    stop_loss: Number($('stopLoss').value),
    cooldown_days: Number($('cooldownDays').value),
    vote_threshold: Number($('voteThreshold').value),
    rf: Number($('rf').value),
  };
}

async function runBacktest() {
  if (S.running) return;
  const payload = collectPayload();
  if (!/^\d{6}$/.test(payload.symbol)) {
    showMessage('error', '<strong>请输入 6 位股票代码</strong><br>可以从搜索联想列表中选择，或直接输入如 600519。');
    return;
  }
  S.running = true;
  $('runBtn').disabled = true;
  $('runBtn').textContent = '回测中…';
  clearMessage();
  $('result').classList.add('hidden');
  animateStages();
  try {
    const r = await fetchJSON('/api/backtest', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });
    finishStages(r.status === 'OK');
    if (r.status !== 'OK') {
      const notes = (r.degradation_notes || []).slice(0, 4).map((n) => `<li>${esc(n)}</li>`).join('');
      showMessage(r.status === 'BAD_INPUT' ? 'error' : 'warn',
        `<strong>${esc(r.status)} · ${esc(r.status_message)}</strong><br>` +
        `<span>${esc(r.instruction || '')}</span>` +
        (notes ? `<ul style="margin:8px 0 0;padding-left:20px">${notes}</ul>` : ''));
      return;
    }
    S.report = r;
    renderReport(r);
    $('result').classList.remove('hidden');
    $('result').scrollIntoView({ behavior: 'smooth', block: 'start' });
  } catch (err) {
    finishStages(false);
    showMessage('error', `<strong>请求失败</strong><br>${esc(err.message)}<br>请确认后端服务正在 127.0.0.1:5000 运行。`);
  } finally {
    S.running = false;
    $('runBtn').disabled = false;
    $('runBtn').textContent = '运行回测';
  }
}

/* ------------------------------------------------------------ 结果渲染 */
function renderReport(r) {
  const m = r.meta;
  $('resultTitle').textContent = `${m.name}（${m.symbol}）· 回测结果`;
  $('resultSub').textContent =
    `${m.date_window.start} ~ ${m.date_window.end} · 基准 ${m.benchmark_name}` +
    ` · ${r.execution.trade_count} 笔交易 · 运行签名 ${m.run_signature}` +
    (r.from_cache ? ' · 结果缓存命中' : '');
  renderKPI(r);
  renderAdvisory(r);
  renderChart(r);
  renderCompare(r);
  renderMeta(r);
  renderTrades(r);
  renderNotes(r);
  $('disclaimer').textContent = r.disclaimer || '';
}

function renderKPI(r) {
  const s = r.metrics.strategy, b = r.metrics.benchmark, c = r.metrics.comparison;
  $('kpiGrid').innerHTML = CORE.map(({ key, label, pct, tag }) => {
    const v = s[key], bv = b[key], diff = c[key] ? c[key].difference : null;
    return `
      <div class="kpi">
        <div class="kpi-label"><span>${esc(label)}</span><span class="tag">${esc(tag)}</span></div>
        <div class="kpi-value ${cls(v)}">${fmt(v, pct)}</div>
        <div class="kpi-foot">
          <span>基准 <b class="${cls(bv)}">${fmt(bv, pct)}</b></span>
          <span>差值 <b class="${cls(diff)}">${fmt(diff, pct)}</b></span>
        </div>
      </div>`;
  }).join('');
}

/* ------------------------------------------------------------ 投资研究建议（规则引擎输出） */
const LEVEL_CLASS = {
  insufficient: 'insufficient', caution: 'caution',
  neutral: 'neutral', constructive: 'constructive',
};
const TONE_CLASS = { pos: 'pos', neg: 'neg', neu: 'neu' };
const PRIO_CLASS = { '高': 'high', '中': 'mid', '常规': 'low' };

function renderAdvisory(r) {
  const a = r.advisory;
  const card = $('advisoryCard');
  card.classList.remove('hidden');
  if (!a || a.available === false) {
    $('advisoryBody').innerHTML =
      `<div class="adv-withheld"><b>${esc((a && a.status) || 'WITHHOLD')} · 未生成建议</b>` +
      `<p>${esc((a && a.message) || '本次运行未产出有效回测结果，不生成任何评估或研究建议。')}</p>` +
      `<p>${esc((a && a.guardrail) || '')}</p></div>`;
    return;
  }

  const v = a.verdict || {}, sc = a.score || {};
  const lvl = LEVEL_CLASS[v.level] || 'insufficient';
  const applicable = sc.applicable !== false;
  const total = Number(sc.total);
  const max = Number(sc.max) || 100;
  const ratio = applicable && Number.isFinite(total)
    ? Math.max(0, Math.min(1, total / max)) : 0;
  const R = 44, C = 2 * Math.PI * R;
  const ringColor = {
    insufficient: '#98a2b3', caution: '#b54708',
    neutral: '#2563eb', constructive: '#6941c6',
  }[lvl] || '#2563eb';
  const ring = `
    <svg class="adv-ring" viewBox="0 0 110 110" role="img" aria-label="综合证据分">
      <circle cx="55" cy="55" r="${R}" fill="none" stroke="#eef1f6" stroke-width="9"/>
      <circle cx="55" cy="55" r="${R}" fill="none" stroke="${ringColor}" stroke-width="9"
              stroke-linecap="round" stroke-dasharray="${C.toFixed(1)}"
              stroke-dashoffset="${(C * (1 - ratio)).toFixed(1)}"
              transform="rotate(-90 55 55)" ${applicable ? '' : 'opacity="0.3"'}/>
      <text x="55" y="52" text-anchor="middle" class="adv-ring-text">${applicable ? total.toFixed(0) : '—'}</text>
      <text x="55" y="74" text-anchor="middle" class="adv-ring-sub">${applicable ? `/ ${max}` : '不评分'}</text>
    </svg>`;

  const blockers = (v.blockers || []).length
    ? `<div class="adv-blockers"><b>未达评分门槛</b><ul>${(v.blockers || [])
        .map((x) => `<li>${esc(x)}</li>`).join('')}</ul></div>` : '';
  const gaps = (v.evidence_gaps || []).length
    ? `<div class="adv-gaps"><b>证据缺口</b><ul>${(v.evidence_gaps || [])
        .map((x) => `<li>${esc(x)}</li>`).join('')}</ul></div>` : '';

  const dims = (sc.dimensions || []).map((d) => {
    const dmax = Number(d.max) || 1;
    const pct = Math.max(0, Math.min(100, Number(d.score) / dmax * 100));
    const lc = ['good', 'mid', 'weak', 'unknown'].includes(d.level) ? d.level : 'weak';
    return `<div class="adv-dim">
      <div class="adv-dim-head"><b>${esc(d.label)}</b><span>${Number(d.score).toFixed(1)} / ${dmax}</span></div>
      <div class="adv-bar"><i class="bar-${lc}" style="width:${pct.toFixed(1)}%"></i></div>
      <p class="adv-evidence">${esc(d.evidence)}</p>
      <p class="adv-advice">研究提示：${esc(d.advice)}</p>
    </div>`;
  }).join('');

  const findings = (a.highlights || []).map((h) =>
    `<li class="adv-tone-${TONE_CLASS[h.tone] || 'neu'}"><i></i><span>${esc(h.text)}</span></li>`).join('');
  const actions = (a.actions || []).map((x) =>
    `<li><span class="adv-prio ${PRIO_CLASS[x.priority] || 'low'}">${esc(x.priority)}</span>
       <div><b>${esc(x.title)}</b><p>${esc(x.detail)}</p></div></li>`).join('');
  const risks = (a.risk_flags || []).map((x) => `<li>${esc(x)}</li>`).join('');

  $('advisoryBody').innerHTML = `
    <div class="adv-top">
      <div class="adv-verdict">
        <span class="adv-badge lvl-${lvl}">${esc(v.label)}</span>
        <p class="adv-summary">${esc(v.summary)}</p>
        <p class="adv-scope">${esc(a.scope)}</p>
        ${blockers}${gaps}
      </div>
      <div class="adv-score">
        ${ring}
        <div class="adv-score-cap">${applicable ? '综合证据分' : '本次不评分'}</div>
        <div class="adv-score-note">${esc(sc.applicability_note || '')}</div>
      </div>
    </div>

    <div class="adv-sec-title">五维证据明细 <span>分数仅供证据归纳，不是收益预测</span></div>
    <div class="adv-dims">${dims}</div>

    <div class="adv-sec-title">关键发现 <span>均为本次回测已产出数值的事实陈列</span></div>
    <ul class="adv-findings">${findings}</ul>

    <div class="adv-sec-title">下一步研究动作 <span>研究流程建议，不含买卖操作意见</span></div>
    <ol class="adv-actions">${actions}</ol>

    <div class="adv-risk"><b>风险与边界</b><ul>${risks}</ul></div>

    <div class="adv-foot">
      <p class="adv-guardrail">${esc(a.guardrail || '')}</p>
      <details>
        <summary>评分方法与完整免责声明</summary>
        <p>${esc(sc.methodology || '')}</p>
        <p>${esc(a.disclaimer || '')}</p>
        <p class="adv-sig">运行签名 ${esc((a.generated_from || {}).run_signature || (r.meta || {}).run_signature || '')}</p>
      </details>
    </div>`;
}

function renderCompare(r) {
  const { strategy, benchmark, buy_hold, comparison } = r.metrics;
  $('benchLabel').textContent = `${r.meta.benchmark_name} (${r.meta.benchmark})`;
  const rows = CORE.concat([{ key: 'annualized_volatility', label: '年化波动率', pct: true }]);
  const body = rows.map(({ key, label, pct }) => {
    const sv = strategy[key], bv = benchmark[key], hv = buy_hold[key];
    const diff = comparison[key] ? comparison[key].difference
      : (sv !== null && bv !== null ? sv - bv : null);
    return `<tr>
      <td>${esc(label)}</td>
      <td class="cell-strong ${cls(sv)}">${fmt(sv, pct)}</td>
      <td class="${cls(bv)}">${fmt(bv, pct)}</td>
      <td class="${cls(hv)}">${fmt(hv, pct)}</td>
      <td class="${cls(diff)}">${fmt(diff, pct)}</td>
    </tr>`;
  }).join('');
  $('compareTable').innerHTML = `
    <table><thead><tr><th>指标</th><th>策略</th><th>${esc(r.meta.benchmark_name)}</th><th>买入持有</th><th>策略−基准</th></tr></thead>
    <tbody>${body}</tbody></table>
    <p class="chart-note">${esc(comparison.note || '')}</p>`;
}

function renderMeta(r) {
  const m = r.meta, q = r.data_quality || {}, sum = r.signal_summary || {}, ex = r.execution || {};
  const cov = (m.data_coverage || {});
  const bcov = (m.benchmark_coverage || {});
  $('signatureLabel').textContent = `run_id ${m.run_signature}`;
  const rows = [
    ['标的', `${m.symbol} · ${m.name}`],
    ['回测区间', `${m.date_window.start} ~ ${m.date_window.end}（含 ${(cov.rows || 0)} 个预热交易日）`],
    ['as_of 上界', `${m.as_of}（服务端强制钳制，前端无法取到未来数据）`],
    ['个股数据源', m.data_vendor],
    ['基准数据源', m.benchmark_vendor],
    ['数据覆盖', `个股 ${cov.first_date || '—'} ~ ${cov.last_date || '—'}；基准 ${bcov.first_date || '—'} ~ ${bcov.last_date || '—'}`],
    ['信号概况', `BUY ${sum.buy_days || 0} / SELL ${sum.sell_days || 0} / HOLD ${sum.hold_days || 0} / REVIEW ${sum.review_days || 0}`],
    ['执行概况', `${ex.trade_count || 0} 笔交易 · ${ex.force_sell_count || 0} 次止损 · ${ex.review_no_action_days || 0} 个 REVIEW 不动作日`],
    ['关键参数', `MA${m.parameters.fast_window}/MA${m.parameters.slow_window} · 仓位 ${(m.parameters.max_position * 100).toFixed(0)}% · 止损 ${(m.parameters.stop_loss * 100).toFixed(0)}% · 无风险利率 ${(m.parameters.rf * 100).toFixed(1)}%`],
    ['版本', `${m.app_version} · 生成于 ${m.generated_at}`],
  ];
  $('metaBox').innerHTML = `<dl class="meta-grid">${rows.map(([k, v], i) =>
    `<dt>${esc(k)}</dt><dd class="${i === 3 || i === 4 ? 'meta-vendor' : ''}">${esc(v)}</dd>`).join('')}</dl>`;
}

function renderTrades(r) {
  const trades = r.trades || [];
  $('tradeCount').textContent = `共 ${trades.length} 笔${trades.length > 120 ? '（显示最近 120 笔）' : ''}`;
  if (!trades.length) {
    $('tradesBox').innerHTML = '<div class="empty">本区间内没有产生交易（可能全部信号为 HOLD 或 REVIEW）。</div>';
    return;
  }
  const shown = trades.slice(-120).reverse();
  $('tradesBox').innerHTML = `
    <table><thead><tr><th>日期</th><th>方向</th><th>成交价</th><th>数量</th><th>金额</th><th>费用</th><th>目标仓位</th><th>触发原因</th></tr></thead>
    <tbody>${shown.map((t) => `<tr>
      <td>${esc(t.date)}</td>
      <td class="${t.side === 'BUY' ? 'buy' : 'sell'}">${t.side === 'BUY' ? '买入' : '卖出'}</td>
      <td>${Number(t.price).toFixed(2)}</td>
      <td>${Number(t.shares).toFixed(2)}</td>
      <td>${Number(t.amount).toFixed(2)}</td>
      <td>${Number(t.fee).toFixed(2)}</td>
      <td>${(Number(t.target_position) * 100).toFixed(0)}%</td>
      <td class="reason"><span title="${esc(t.reason)}">${esc(String(t.reason).slice(0, 24))}</span></td>
    </tr>`).join('')}</tbody></table>`;
}

function renderNotes(r) {
  const notes = r.degradation_notes || [];
  $('notesCount').textContent = `共 ${notes.length} 条`;
  $('notesList').innerHTML = notes.length
    ? notes.map((n) => `<li>${esc(n)}</li>`).join('')
    : '<li>本次运行无降级记录。</li>';
}

/* ------------------------------------------------------------ 净值曲线（自绘 SVG，离线可用） */
function renderLegend() {
  $('legend').innerHTML = Object.keys(COLORS).map((key) => `
    <button data-key="${key}" class="${S.visible[key] ? '' : 'off'}">
      <i style="background:${COLORS[key]}"></i>${esc(SERIES_LABEL[key])}
    </button>`).join('');
  $('legend').querySelectorAll('button').forEach((btn) => {
    btn.addEventListener('click', () => {
      const key = btn.dataset.key;
      if (S.visible[key] && Object.values(S.visible).filter(Boolean).length === 1) return;
      S.visible[key] = !S.visible[key];
      renderLegend();
      if (S.report) renderChart(S.report);
    });
  });
}

function renderChart(r) {
  const svg = $('chart');
  const wrap = $('chartWrap');
  const W = Math.max(620, Math.round(wrap.clientWidth || 1000));
  const H = 380;
  const pad = { l: 66, r: 18, t: 18, b: 34 };
  svg.setAttribute('viewBox', `0 0 ${W} ${H}`);
  svg.setAttribute('width', '100%');
  svg.setAttribute('height', String(H));

  const dates = r.curves.dates || [];
  const n = dates.length;
  if (n < 2) { svg.innerHTML = ''; return; }
  const series = [
    { key: 'strategy', data: r.curves.strategy, width: 2.4 },
    { key: 'benchmark', data: r.curves.benchmark, width: 1.8 },
    { key: 'buy_hold', data: r.curves.buy_hold, width: 1.6 },
  ];
  const visible = series.filter((s) => S.visible[s.key]);
  let lo = Infinity, hi = -Infinity;
  visible.forEach((s) => s.data.forEach((v) => {
    if (Number.isFinite(v)) { lo = Math.min(lo, v); hi = Math.max(hi, v); }
  }));
  if (!Number.isFinite(lo)) return;
  const margin = (hi - lo) * 0.08 || Math.max(hi * 0.01, 1);
  lo -= margin; hi += margin;
  const x = (i) => pad.l + (i / (n - 1)) * (W - pad.l - pad.r);
  const y = (v) => pad.t + (1 - (v - lo) / (hi - lo)) * (H - pad.t - pad.b);

  let html = '';
  for (let g = 0; g <= 5; g += 1) {
    const v = lo + (hi - lo) * g / 5;
    const yy = y(v);
    html += `<line x1="${pad.l}" y1="${yy.toFixed(1)}" x2="${W - pad.r}" y2="${yy.toFixed(1)}" stroke="#eef1f6" stroke-width="1"/>`;
    html += `<text x="${pad.l - 10}" y="${(yy + 4).toFixed(1)}" text-anchor="end" font-size="11" fill="#98a2b3">${fmtMoney(v)}</text>`;
  }
  const initial = Number(r.meta.parameters.initial_capital);
  if (initial > lo && initial < hi) {
    const y0 = y(initial);
    html += `<line x1="${pad.l}" y1="${y0.toFixed(1)}" x2="${W - pad.r}" y2="${y0.toFixed(1)}" stroke="#d0d5dd" stroke-width="1" stroke-dasharray="5 4"/>`;
    html += `<text x="${W - pad.r - 4}" y="${(y0 - 6).toFixed(1)}" text-anchor="end" font-size="10.5" fill="#98a2b3">初始资金</text>`;
  }
  for (let k = 0; k < 6; k += 1) {
    const i = Math.round(k * (n - 1) / 5);
    html += `<text x="${x(i).toFixed(1)}" y="${H - 11}" text-anchor="middle" font-size="11" fill="#98a2b3">${esc(dates[i].slice(2))}</text>`;
  }
  visible.forEach((s) => {
    const pts = s.data.map((v, i) => `${x(i).toFixed(2)},${y(v).toFixed(2)}`).join(' ');
    html += `<polyline points="${pts}" fill="none" stroke="${COLORS[s.key]}" stroke-width="${s.width}" stroke-linejoin="round" stroke-linecap="round"/>`;
  });
  html += `<line id="hoverLine" x1="0" y1="${pad.t}" x2="0" y2="${H - pad.b}" stroke="#98a2b3" stroke-width="1" stroke-dasharray="3 3" style="display:none"/>`;
  visible.forEach((s) => {
    html += `<circle id="dot-${s.key}" r="4" fill="${COLORS[s.key]}" stroke="#fff" stroke-width="2" style="display:none"/>`;
  });
  html += `<rect id="hoverCatch" x="${pad.l}" y="${pad.t}" width="${W - pad.l - pad.r}" height="${H - pad.t - pad.b}" fill="transparent"/>`;
  svg.innerHTML = html;

  const hoverLine = $('hoverLine');
  const tooltip = $('tooltip');
  const catcher = $('hoverCatch');
  catcher.addEventListener('mousemove', (e) => {
    const rect = svg.getBoundingClientRect();
    const px = (e.clientX - rect.left) / rect.width * W;
    let i = Math.round((px - pad.l) / (W - pad.l - pad.r) * (n - 1));
    i = Math.max(0, Math.min(n - 1, i));
    const xx = x(i);
    hoverLine.setAttribute('x1', xx.toFixed(1));
    hoverLine.setAttribute('x2', xx.toFixed(1));
    hoverLine.style.display = '';
    visible.forEach((s) => {
      const dot = $(`dot-${s.key}`);
      dot.setAttribute('cx', xx.toFixed(1));
      dot.setAttribute('cy', y(s.data[i]).toFixed(1));
      dot.style.display = '';
    });
    const wrapRect = wrap.getBoundingClientRect();
    tooltip.innerHTML = `<div class="tt-date">${esc(dates[i])}</div>` + visible.map((s) => {
      const ret = s.data[i] / initial - 1;
      return `<div class="tt-row"><span>${esc(SERIES_LABEL[s.key])}</span><b class="${cls(ret)}">${Number(s.data[i]).toFixed(0)} (${fmt(ret, true)})</b></div>`;
    }).join('');
    tooltip.style.left = `${Math.max(8, Math.min(e.clientX - wrapRect.left + 14, wrapRect.width - 196))}px`;
    tooltip.style.top = '18px';
    tooltip.classList.remove('hidden');
  });
  catcher.addEventListener('mouseleave', () => {
    hoverLine.style.display = 'none';
    visible.forEach((s) => { $(`dot-${s.key}`).style.display = 'none'; });
    tooltip.classList.add('hidden');
  });
  S.chart = { W, H, pad, x, y, visible, dates, initial };
}

/* 深链接：/?autorun=1&symbol=600519 打开即自动回测，便于分享带结果的链接 */
async function bootstrap() {
  await init();
  const q = new URLSearchParams(location.search);
  if (q.get('autorun') !== '1') return;
  const sym = q.get('symbol');
  if (sym && /^\d{6}$/.test(sym)) $('symbolInput').value = sym;
  if (q.get('start')) $('startDate').value = q.get('start');
  if (q.get('end')) $('endDate').value = q.get('end');
  if (q.get('benchmark')) $('benchmark').value = q.get('benchmark');
  setTimeout(runBacktest, 120);
}

bootstrap();
