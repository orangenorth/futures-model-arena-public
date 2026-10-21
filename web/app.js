/* 四模型期货竞赛看板：5 秒轮询 /api/state，权益曲线 15 秒刷新 */
(function () {
  const $ = (s) => document.querySelector(s);
  const state = {
    data: null, range: 'all', pct: false, trader: null, tab: 'positions', page: 0, pageSize: 30, colors: {}, names: {},
    debate: null, debateList: [], debateId: null, debateSelectedByUser: false, debateRequest: 0, debateStarting: false, competitionId: null,
    stateRequest: 0,
    universe: null, universeFilter: '', admission: null,
    researchScan: null, researchScanStarting: false,
    productResearch: null, productResearchPage: 0, productResearchPageSize: 20, productResearchStarting: false,
    productResearchRunId: (() => {
      const runId = Number(new URLSearchParams(window.location.search).get('product_research_run'));
      return Number.isInteger(runId) && runId > 0 ? runId : null;
    })(),
    modalTrigger: null,
  };
  function rememberProductResearchRun(runId) {
    const normalized = Number(runId);
    state.productResearchRunId = Number.isInteger(normalized) && normalized > 0 ? normalized : null;
    const url = new URL(window.location.href);
    if (state.productResearchRunId) url.searchParams.set('product_research_run', String(state.productResearchRunId));
    else url.searchParams.delete('product_research_run');
    window.history.replaceState(null, '', url.toString());
  }
  const fmt = (n, d = 0) => (n === null || n === undefined || isNaN(n)) ? '-' : Number(n).toLocaleString('zh-CN', { minimumFractionDigits: d, maximumFractionDigits: d });
  const fmtPx = (n) => (n === null || n === undefined) ? '-' : (Math.abs(n) >= 1000 ? fmt(n, 0) : Math.abs(n) >= 10 ? fmt(n, 2) : fmt(n, 4));
  const pct = (n, d = 2) => (n === null || n === undefined) ? '-' : (n >= 0 ? '+' : '') + Number(n).toFixed(d) + '%';
  const cls = (n) => (n > 0 ? 'pos' : n < 0 ? 'neg' : '');
  const formatTime = (t, options = {}) => {
    if (!t) return '-';
    const timeZone = state.data?.competition?.timezone;
    return new Date(t * 1000).toLocaleString('zh-CN', { hour12: false, ...(timeZone ? { timeZone } : {}), ...options });
  };
  const ts = (t) => formatTime(t);
  const tsShort = (t) => formatTime(t, { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', second: '2-digit' });
  const actName = { open_long: '开多', open_short: '开空', close: '平仓', close_long: '平多', close_short: '平空', adjust_sl_tp: '调整止损止盈', hold: '观望', stop_loss: '止损', take_profit: '止盈', liquidation: '强平' };
  const esc = (s) => String(s ?? '').replace(/[&<>"]/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
  const safeColor = (value) => /^#[0-9a-f]{3,8}$/i.test(String(value || '')) ? value : '#3b82f6';
  // 与引擎 DOMESTIC_MARKETS 一致：CTP 只是执行行情源，全品种研究走自己的公开目录读取器。
  function isDomesticMarket(market) {
    return ['cn_futures', 'ctp'].includes(market);
  }
  const decisionFailureMessages = Object.freeze({
    timeout: '模型请求超时，已停止本轮重试',
    rate_limited: '模型请求频率或可用额度受限，已停止本轮重试',
    unknown: '模型调用失败，未展示供应商原始错误',
    circuit_breaker: '熔断恢复中，暂不发起模型请求',
  });
  const decisionFailureMessage = (category) => decisionFailureMessages[category] || '';

  async function api(path, opts) {
    const r = await fetch(path, opts);
    if (!r.ok) {
      let msg = path + ' ' + r.status;
      try { const j = await r.json(); if (j && j.detail) msg = j.detail; } catch (e) { /* 非 JSON 错误体 */ }
      throw new Error(msg);
    }
    return r.json();
  }

  // ---------- 顶栏 + 排行榜 ----------
  function renderTop(d) {
    $('#comp-name').textContent = d.competition.name;
    document.title = d.competition.name;
    $('#comp-market').textContent = { cn_futures: '国内商品期货', ctp: '国内商品期货（CTP）', crypto: '加密永续合约', replay: '合成行情（演示）' }[d.competition.market] || d.competition.market;
    $('#round-no').textContent = d.competition.round_no;
    $('#started-at').textContent = ts(d.competition.started_at);
    const st = d.status;
    const pill = $('#status-pill');
    pill.textContent = st.text;
    pill.className = 'pill ' + (st.paused ? 'paused' : st.text.includes('决策') ? 'busy' : st.market_open ? 'run' : 'closed');
    $('#countdown').textContent = st.paused ? '已暂停' : (st.market_open ? st.next_decision_in + 's' : (st.next_open ? '开盘后' : '-'));
    const src = st.source || {};
    const health = $('#source-health');
    const detail = src.detail ? String(src.detail) : '';
    // 异常时把行情源给出的原因（例如 Binance 451 地区限制、建议改用 OKX）直接显示，完整文本悬停可见
    health.textContent = (src.ok ? '正常' : '异常') + (src.errors ? ` (错误 ${src.errors})` : '')
      + (!src.ok && detail ? `：${detail.length > 60 ? detail.slice(0, 60) + '…' : detail}` : '');
    health.title = detail;
    health.className = src.ok ? 'pos' : 'neg';
    $('#last-tick').textContent = st.last_tick ? tsShort(st.last_tick) : '-';
    $('#btn-pause').textContent = st.paused ? '继续' : '暂停';
  }

  function renderCards(d) {
    const cur = d.competition.currency;
    const risk = d.competition.risk || {};
    const riskCap = risk.max_margin_risk_ratio;
    const targetMin = risk.target_margin_risk_min_ratio;
    const targetMax = risk.target_margin_risk_max_ratio;
    const html = d.traders.map((t) => {
      const color = safeColor(t.color);
      state.colors[t.id] = color; state.names[t.id] = t.name;
      const br = t.breaker || {};
      const badge = t.mode === 'llm' ? '<span class="badge llm">LLM</span>'
        : (t.mode === 'jev' ? '<span class="badge llm">JEV</span>' : '<span class="badge">演示</span>');
      const failure = decisionFailureMessage(br.failure_category);
      const skipRounds = Number(br.skip || 0);
      const breakerSuffix = skipRounds > 0 ? `（剩余跳过 ${skipRounds} 轮）` : '';
      const warn = br.status === 'breaker'
        ? `<span class="badge warn" title="${esc(failure || decisionFailureMessages.circuit_breaker)}">熔断恢复中${breakerSuffix}</span>`
        : (failure ? `<span class="badge warn" title="${esc(failure)}">上轮出错</span>` : '');
      const riskValue = t.risk_ratio == null ? '-' : (Number(t.risk_ratio) * 100).toFixed(1) + '%';
      const capLabel = targetMin != null && targetMax != null
        ? `目标 ${(Number(targetMin) * 100).toFixed(0)}–${(Number(targetMax) * 100).toFixed(0)}% / 硬上限 ${riskCap == null ? '-' : (Number(riskCap) * 100).toFixed(0) + '%'}`
        : (riskCap == null ? '未设总上限' : `上限 ${(Number(riskCap) * 100).toFixed(0)}%`);
      const riskStateLabel = {
        below_target: '低于目标（可观望）', in_target: '目标区', target_ceiling: '目标上沿（不新增风险）',
        above_target: '警戒区（不新增风险）',
        normal: '正常', warning: '预警', cap: '硬上限', unknown: '未知',
      }[t.risk_state] || '';
      const riskClass = ['below_target', 'in_target', 'target_ceiling', 'above_target', 'normal', 'warning', 'cap'].includes(t.risk_state) ? `risk-${t.risk_state}` : '';
      return `<div class="card" style="border-top-color:${color}">
        <div class="rank ${t.rank === 1 ? 'r1' : ''}">#${t.rank}</div>
        <div class="name">${esc(t.name)} ${badge} ${warn}</div>
        <div class="model">${esc(t.model || '')}</div>
        <div><span class="equity">${fmt(t.equity)}</span> <span class="muted">${cur}</span><span class="ret ${cls(t.return_pct)}">${pct(t.return_pct)}</span></div>
        <div class="kv">
          <div><span>今日盈亏</span><b class="${cls(t.today_pnl)}">${fmt(t.today_pnl)}</b></div>
          <div><span>最大回撤</span><b>${t.max_drawdown != null ? t.max_drawdown.toFixed(2) + '%' : '-'}</b></div>
          <div><span>Sharpe</span><b>${t.sharpe != null ? t.sharpe.toFixed(2) : '-'}</b></div>
          <div><span>胜率</span><b>${t.win_rate != null ? t.win_rate.toFixed(0) + '%' : '-'}</b> <span style="display:inline" class="muted">(${t.closed_trades ?? 0}笔)</span></div>
          <div><span>持仓</span><b>${t.positions.length}</b> <span style="display:inline" class="muted">占用 ${fmt(t.margin_used)}</span></div>
          <div><span>风险度</span><b class="risk-value ${riskClass}" title="${esc(riskStateLabel)}">${riskValue}</b> <span style="display:inline" class="muted">${capLabel}${riskStateLabel ? ` · ${riskStateLabel}` : ''}</span></div>
          <div><span>浮动盈亏</span><b class="${cls(t.unrealized)}">${fmt(t.unrealized)}</b></div>
          <div><span>已实现</span><b class="${cls(t.realized_pnl)}">${fmt(t.realized_pnl)}</b></div>
          <div><span>手续费</span><b>${fmt(t.fees)}</b></div>
          <div><span>强平次数</span><b>${t.liquidations ?? 0}</b></div>
        </div>
      </div>`;
    }).join('');
    $('#leaderboard').innerHTML = html;
    // trader tabs
    const tabs = $('#trader-tabs');
    if (!state.trader && d.traders.length) state.trader = d.traders[0].id;
    tabs.innerHTML = d.traders.slice().sort((a, b) => a.name.localeCompare(b.name)).map((t) => `<button data-trader="${esc(t.id)}" class="${t.id === state.trader ? 'on' : ''}" style="border-color:${t.id === state.trader ? safeColor(t.color) : ''}">${esc(t.name)}</button>`).join('');
  }

  function renderQuotes(d) {
    const tb = $('#quotes-tbl tbody');
    tb.innerHTML = d.quotes.map((q) => `<tr><td>${esc(q.symbol)} <span class="muted">${esc(q.name)}</span></td><td>${fmtPx(q.price)}</td><td class="${cls(q.change_pct)}">${pct(q.change_pct)}</td><td>${q.funding_rate != null ? (q.funding_rate * 100).toFixed(4) + '%' : '-'}</td></tr>`).join('') || '<tr><td colspan="4" class="empty">等待行情…</td></tr>';
  }

  // ---------- 品种宇宙（只读候选目录） ----------
  // 目录是静态发现产物：只在启动时取一次，不进入 /api/state 的 5 秒轮询，
  // 也不会让候选代码走行情、快照或 prompt 路径。
  const flag = (v) => v === true ? '<span class="flag-yes">可用</span>'
    : v === false ? '<span class="flag-no">不可用</span>'
      : '<span class="flag-unknown">未探测</span>';
  const mark = (v) => v ? '<span class="flag-yes">是</span>' : '<span class="flag-unknown">否</span>';
  // 本期没有做任何交易所条款核验，两种取值都只说明本地留存了多少材料。
  const verify = (v) => v === 'legacy_unverified'
    ? '<span class="flag-unknown">未核验（仅本地旧规格）</span>'
    : '<span class="flag-unknown">未核验</span>';
  const snapshot = (v) => v === 'complete' ? '完整' : v === 'partial' ? '不完整' : '未知';
  // The ledger says how far term evidence has been prepared. It is not exchange
  // verification and not an authorisation to trade, so the labels never say so.
  const admissionLabel = (v) => v === 'ready_for_review' ? '<span class="flag-unknown">待人工复核</span>'
    : v === 'blocked' ? '<span class="flag-no">已阻断</span>'
      : v === 'pending' ? '<span class="flag-unknown">未开始</span>'
        : '<span class="flag-unknown">无台账</span>';
  const coverage = (r) => r ? `${r.evidence_recorded}/${r.evidence_required}` : '-';

  function renderUniverse() {
    const u = state.universe;
    const tb = $('#universe-tbl tbody');
    if (!tb) return;
    const ledger = (state.admission && state.admission.available) ? state.admission : null;
    const prepared = {};
    if (ledger) (ledger.records || []).forEach((r) => { prepared[r.symbol] = r; });
    $('#admission-summary').textContent = ledger
      ? `未开始 ${ledger.counts.pending} · 已阻断 ${ledger.counts.blocked} · 待人工复核 ${ledger.counts.ready_for_review}`
      : '未建立';
    if (!u || !u.available) {
      $('#universe-count').textContent = '0';
      $('#universe-shown').textContent = '';
      tb.innerHTML = '<tr><td colspan="12" class="empty">尚未生成候选目录（运行 scripts/discover_contract_universe.py）</td></tr>';
      return;
    }
    $('#universe-source').textContent = u.source || '-';
    $('#universe-fetched').textContent = u.fetched_at || '-';
    $('#universe-count').textContent = u.count;
    $('#universe-snapshot').textContent = snapshot(u.snapshot_status);
    if (u.note) $('#universe-note').title = u.note;
    const q = state.universeFilter.trim().toLowerCase();
    const rows = (u.candidates || []).filter((c) => !q
      || String(c.symbol).toLowerCase().includes(q)
      || String(c.name).toLowerCase().includes(q)
      || String(c.exchange).toLowerCase().includes(q));
    $('#universe-shown').textContent = q ? `匹配 ${rows.length} / ${u.count}` : `共 ${u.count} 个候选`;
    tb.innerHTML = rows.map((c) => `<tr><td>${esc(c.symbol)}</td><td class="left">${esc(c.name)}</td><td>${esc(c.exchange)}</td>`
      + `<td>${flag(c.quote_ok)}</td><td>${flag(c.history_5m_ok)}</td><td>${flag(c.history_15m_ok)}</td><td>${flag(c.history_1h_ok)}</td>`
      + `<td>${mark(c.configured_spec)}</td><td>${mark(c.active_in_competition)}</td>`
      + `<td>${verify(c.verification_status)}</td>`
      + `<td>${admissionLabel(prepared[c.symbol] && prepared[c.symbol].admission_state)}</td>`
      + `<td>${coverage(prepared[c.symbol])}</td></tr>`).join('')
      || '<tr><td colspan="12" class="empty">没有匹配的候选品种</td></tr>';
  }

  async function loadUniverse() {
    try {
      state.universe = await api('/api/universe');
    } catch (e) {
      state.universe = null;
    }
    // The ledger is a second static artefact: fetched once, and a failure only
    // degrades its two columns rather than hiding the catalogue itself.
    try {
      state.admission = await api('/api/universe/admission');
    } catch (e) {
      state.admission = null;
    }
    renderUniverse();
  }

  // ---------- Full-universe technical scan (research-only; outside state polling and model facts) ----------
  const scanStatusLabel = (status, catalogueStatus) => {
    if (status === 'incomplete') {
      return catalogueStatus === 'complete' ? '本轮扫描未完成（超时或取消）' : '目录不完整';
    }
    return ({ complete: '全量分析完成', partial: '存在排除项', unavailable: '目录不可用' }[status] || '尚未扫描');
  };
  const scanTrendLabel = (trend) => ({ bullish: '偏多', bearish: '偏空', neutral: '中性' }[trend] || '-');
  const hasFullEligibleCoverage = (scan) => {
    const counts = scan?.counts || {};
    const items = Array.isArray(scan?.items) ? scan.items : [];
    const exclusions = items.filter((item) => item?.outcome === 'excluded');
    return scan?.status === 'partial'
      && Number(counts.eligible) > 0
      && Number(counts.scanned) === Number(counts.eligible)
      && Number(counts.scanned) + Number(counts.excluded) === Number(counts.discovered)
      && exclusions.length === Number(counts.excluded)
      && exclusions.every((item) => item?.stage === 'catalogue');
  };

  function renderResearchScan() {
    const payload = state.researchScan || {};
    const latest = payload.latest || null;
    const running = Boolean(payload.running || state.researchScanStarting);
    const status = latest?.status || (running ? 'running' : 'idle');
    const eligibleCovered = hasFullEligibleCoverage(latest);
    const statusEl = $('#research-scan-status');
    const trigger = $('#btn-research-scan');
    if (!statusEl || !trigger) return;
    statusEl.textContent = running ? '全量扫描进行中' : eligibleCovered ? '可研究范围覆盖完成（目录保留缺口）' : scanStatusLabel(status, latest?.catalogue?.snapshot_status);
    statusEl.className = 'research-status ' + (running ? 'running' : status === 'complete' || eligibleCovered ? 'ready' : ['partial', 'incomplete', 'unavailable'].includes(status) ? 'warn' : '');
    trigger.disabled = running;
    trigger.textContent = running ? '正在扫描…' : '更新全量扫描';
    trigger.title = running ? '本轮公开行情读取仍在进行。' : '读取已校验候选目录与公开行情；不会调用模型、不会下单、不会修改持仓。';

    const summary = $('#research-scan-summary');
    const table = $('#research-scan-tbl tbody');
    if (!latest) {
      summary.innerHTML = '<div class="research-empty">尚无全量扫描结果。手动更新会按候选目录的实际数量扫描，不会把配置交易池当作分母。</div>';
      table.innerHTML = '<tr><td colspan="8" class="empty">尚无扫描明细</td></tr>';
      return;
    }
    const counts = latest.counts || {};
    const catalogue = latest.catalogue || {};
    const coverage = latest.coverage || {};
    const market = latest.market || null;
    const items = Array.isArray(latest.items) ? latest.items : [];
    const exclusions = items.filter((item) => item?.outcome === 'excluded');
    const ratio = coverage.ratio == null ? '未知' : `${(Number(coverage.ratio) * 100).toFixed(1)}%`;
    const eligibleRatio = Number(counts.eligible) > 0
      ? `${(Number(counts.scanned) / Number(counts.eligible) * 100).toFixed(1)}%` : '未知';
    const catalogueState = catalogue.snapshot_status === 'complete' ? '完整' : catalogue.snapshot_status === 'partial' ? '不完整' : '未知';
    const exclusionsHtml = exclusions.length
      ? `<details class="scan-exclusions"><summary>查看 ${fmt(exclusions.length, 0)} 个排除原因</summary><ul>${exclusions.map((item) => `<li><b>${esc(item.symbol)}</b> · ${esc(item.stage)} / ${esc(item.reason)}</li>`).join('')}</ul></details>`
      : '<p class="research-meta">本轮没有排除项。</p>';
    const freshnessLabel = ({ live_session: '盘中实时快照', prior_session: '上一已收盘会话快照（非盘中实时）', mixed: '混合会话快照' }[market?.freshness_mode] || '未提供');
    const asOfRange = (first, last) => (Number.isFinite(Number(first)) && Number.isFinite(Number(last)))
      ? (Number(first) === Number(last) ? tsShort(first) : `${tsShort(first)} 至 ${tsShort(last)}`) : '未提供';
    const marketHtml = market
      ? `<p>行情有效时间：${esc(freshnessLabel)} · 报价 ${esc(asOfRange(market.quote_as_of_min, market.quote_as_of_max))} · 5m 技术数据 ${esc(asOfRange(market.history_as_of_min, market.history_as_of_max))}。</p>`
      : '<p>行情有效时间：本轮没有形成可用的时间证明。</p>';
    const scopeNote = eligibleCovered
      ? `<p>可研究范围已覆盖：${fmt(counts.eligible, 0)} 个目录证明通过合约均已分析；其余 ${fmt(counts.excluded, 0)} 个仅为目录公开报价证明缺口，不会缩减已验证的研究范围。</p>`
      : '';
    const scanLabel = eligibleCovered ? '可研究范围覆盖完成（目录保留缺口）' : scanStatusLabel(latest.status, catalogue.snapshot_status);
    summary.innerHTML = `<div class="research-metrics"><span class="metric-chip">发现 ${fmt(counts.discovered, 0)}</span><span class="metric-chip">目录证明通过 ${fmt(counts.eligible, 0)}</span><span class="metric-chip">本轮已分析 ${fmt(counts.scanned, 0)}</span><span class="metric-chip">排除 ${fmt(counts.excluded, 0)}</span><span class="metric-chip">目录覆盖 ${ratio}</span><span class="metric-chip">可研究覆盖 ${eligibleRatio}</span></div><p>目录快照：${esc(catalogueState)} · 发现分母 ${fmt(counts.discovered, 0)}；本轮状态：${esc(scanLabel)}。</p><p>目录时间：${esc(catalogue.fetched_at || '未提供')} · 扫描完成：${esc(tsShort(latest.completed_at))}。</p>${marketHtml}${scopeNote}<p class="research-meta">本面板手动扫描不下单、也不直接调用模型；赛前决策室另建全量候选快照，只有覆盖完整时才将有界汇总与代表性事实用于模型研究。</p><p class="research-meta">清单哈希 ${esc(String(latest.manifest_hash || '').slice(0, 18) || '-')}…。</p>${exclusionsHtml}`;
    table.innerHTML = items.map((item) => `<tr><td>${esc(item.symbol)}</td><td class="left">${esc(item.name)}</td><td>${esc(item.exchange)}</td><td>${item.outcome === 'scanned' ? '<span class="flag-yes">已分析</span>' : '<span class="flag-no">排除</span>'}</td><td>${item.outcome === 'scanned' ? fmtPx(item.latest_price) : '-'}</td><td class="${cls(item.change_pct)}">${item.outcome === 'scanned' ? pct(item.change_pct) : '-'}</td><td>${esc(scanTrendLabel(item.trend))}</td><td>${item.outcome === 'excluded' ? `${esc(item.stage)} / ${esc(item.reason)}` : esc(item.score == null ? '-' : Number(item.score).toFixed(2))}</td></tr>`).join('')
      || '<tr><td colspan="8" class="empty">本轮没有可展示的扫描项</td></tr>';
  }

  async function loadResearchScan() {
    try {
      state.researchScan = await api('/api/research-scan');
    } catch (e) {
      state.researchScan = null;
    }
    renderResearchScan();
  }

  async function waitForResearchScan() {
    // This low-frequency read loop exists only after the user explicitly starts
    // a scan.  It never invokes the scan endpoint, and it is intentionally
    // separate from the five-second competition-state poll below.
    for (let attempt = 0; attempt < 150; attempt += 1) {
      await new Promise((resolve) => setTimeout(resolve, 1000));
      await loadResearchScan();
      if (!state.researchScan?.running) return;
    }
  }

  // ---------- Full-universe product debates (one product, one four-model session) ----------
  const productRunLabel = {
    discovering: '正在刷新品种目录', queued: '等待品种调度', running: '逐品种论证中',
    complete: '全量论证完成', complete_with_exceptions: '已结束，含部分完成或失败项',
    interrupted: '运行中断，可安全续跑', failed: '运行失败',
  };
  const productItemLabel = { excluded: '已排除', pending: '待处理', running: '运行中', complete: '完成', partial: '部分完成', failed: '失败' };
  const productCallLabel = {
    pending: '待处理', submitted: '请求中', complete: '完成', failed: '失败',
    delivery_unknown: '投递状态不确定', skipped: '跳过',
  };
  function latestProductCall(entries) {
    if (!Array.isArray(entries) || !entries.length) return null;
    return entries.reduce((latest, item) => Number(item.attempt || 0) >= Number(latest?.attempt || 0) ? item : latest, entries[0]);
  }
  function productModelCell(item, stage, participants) {
    const byStage = item.models?.[stage] || {};
    return participants.map((participant) => {
      const call = latestProductCall(byStage[participant.id]);
      const status = call?.status || 'pending';
      const title = call?.error ? `${participant.name || participant.id}：${productCallLabel[status] || status} / ${call.error}` : `${participant.name || participant.id}：${productCallLabel[status] || status}`;
      const klass = status === 'complete' ? 'flag-yes' : status === 'pending' || status === 'submitted' ? 'flag-unknown' : 'flag-no';
      return `<span class="product-model-state ${klass}" title="${esc(title)}">${esc(participant.name || participant.id)}·${esc(productCallLabel[status] || status)}${call?.attempt > 1 ? `#${fmt(call.attempt, 0)}` : ''}</span>`;
    }).join('<br>');
  }
  function productFailureSummary(item) {
    const failures = [];
    Object.entries(item.models || {}).forEach(([stage, byTrader]) => {
      Object.entries(byTrader || {}).forEach(([traderId, entries]) => {
        const call = latestProductCall(entries);
        if (!call || ['complete', 'pending', 'submitted'].includes(call.status)) return;
        const label = productCallLabel[call.status] || call.status;
        failures.push(`${stage === 'opening' ? '开场' : '反证'} / ${traderId}：${label}${call.error ? `（${call.error}）` : ''}`);
      });
    });
    return failures.join('；');
  }
  function renderProductResearch() {
    const payload = state.productResearch || {};
    const run = payload.run || null;
    const items = Array.isArray(payload.items) ? payload.items : [];
    const participants = Array.isArray(payload.participants) ? payload.participants : [];
    const statusEl = $('#product-research-status');
    const start = $('#btn-product-research');
    const resume = $('#btn-product-research-resume');
    const retry = $('#btn-product-research-retry');
    const summary = $('#product-research-summary');
    const body = $('#product-research-tbl tbody');
    const page = $('#product-research-page');
    const prev = $('#product-research-prev');
    const next = $('#product-research-next');
    if (!statusEl || !start || !summary || !body) return;
    const readiness = state.data?.product_research?.readiness || {};
    const ready = Boolean(readiness.ready);
    const unavailable = Array.isArray(readiness.unavailable) ? readiness.unavailable : [];
    const historicalRecovery = Boolean(payload.historical_recovery);
    const resumeAllowed = payload.resume_allowed !== false;
    const serviceRunning = Boolean(state.data?.product_research?.running || state.productResearchStarting);
    const active = Boolean(serviceRunning || (!historicalRecovery && ['discovering', 'queued', 'running'].includes(run?.status)));
    const canStart = Boolean(state.data?.status?.running) && isDomesticMarket(state.data?.competition?.market) && ready && !active
      && (!historicalRecovery || payload.resume_allowed === false);
    statusEl.textContent = active ? productRunLabel[run?.status] || '正在初始化' : (productRunLabel[run?.status] || '尚未运行');
    statusEl.className = 'research-status ' + (active ? 'running' : run?.status === 'complete' ? 'ready' : run ? 'warn' : '');
    start.disabled = !canStart;
    start.textContent = state.productResearchStarting ? '正在创建运行…' : '启动全品种论证';
    start.title = canStart
      ? '先刷新分页品种目录与实际行情；每个符合条件品种独立接受四模型开场和反证。不会下单。'
      : (unavailable.length ? `以下模型尚未就绪：${unavailable.join('、')}。为避免把演示策略当作模型论证，不能启动。`
        : (historicalRecovery && payload.resume_allowed ? '已找到可安全续跑的历史研究运行；为避免重复调用，请使用“续跑”。' : '需要国内期货引擎运行，且当前没有进行中的全品种论证。'));
    const resumeStatuses = ['queued', 'running', 'interrupted', 'failed'];
    resume.hidden = !run || serviceRunning || !resumeAllowed || !resumeStatuses.includes(run.status);
    retry.hidden = !run || serviceRunning || !resumeAllowed || !['complete_with_exceptions', 'failed', 'interrupted'].includes(run.status);
    resume.disabled = active; retry.disabled = active;
    if (!run) {
      const readinessNote = ready
        ? '四个真实模型均已就绪。'
        : `尚不能启动：需要恰好四个真实模型；当前未就绪 ${esc(unavailable.join('、') || `配置数 ${fmt(readiness.configured_count, 0)}`)}。不会用演示策略代替缺失模型。`;
      summary.innerHTML = `<div class="research-empty">尚无全品种赛前论证。启动后会动态刷新分页目录，按连续合约 X0 映射逐品种执行；目录或行情缺口会逐项记录，不会静默略过。${readinessNote}</div>`;
      body.innerHTML = '<tr><td colspan="6" class="empty">尚无任务</td></tr>';
      if (page) page.textContent = '';
      return;
    }
    const c = run;
    const mapping = c.mapping_rule || '未提供';
    const source = c.catalogue_hash ? `${String(c.catalogue_hash).slice(0, 18)}…` : '待刷新';
    const counts = `<div class="research-metrics"><span class="metric-chip">目标 ${fmt(c.eligible_count, 0)}</span><span class="metric-chip">排除 ${fmt(c.excluded_count, 0)}</span><span class="metric-chip">待处理 ${fmt(c.pending_count, 0)}</span><span class="metric-chip">运行中 ${fmt(c.running_count, 0)}</span><span class="metric-chip">完成 ${fmt(c.complete_count, 0)}</span><span class="metric-chip failed">部分完成 ${fmt(c.partial_count, 0)}</span><span class="metric-chip failed">失败 ${fmt(c.failed_count, 0)}</span></div>`;
    const unknown = items.flatMap((item) => Object.values(item.models || {}).flatMap((stage) => Object.values(stage || {}).flat())).filter((call) => call.status === 'delivery_unknown').length;
    const recoveryScope = payload.historical_recovery
      ? `<p class="research-meta">恢复范围：该分析运行归属竞赛 #${fmt(c.competition_id, 0)}；当前交易竞赛保持独立。${payload.resume_allowed === false ? `当前配置与原运行不兼容：${esc(payload.resume_reason || '不能安全续跑')}。旧结果保留可查，可启动当前竞赛的新研究运行。` : '仅继续未完成的研究调用，不会写入持仓、订单或排行榜。'}</p>`
      : '';
    summary.innerHTML = `${counts}<p>运行 #${fmt(c.id, 0)} · 发现 ${fmt(c.discovered_count, 0)} 个 · 目录状态 ${esc(c.discovery_status || '待刷新')} · 目录哈希 ${esc(source)} · 扫描记录 ${c.scan_run_id == null ? '待生成' : '#' + fmt(c.scan_run_id, 0)}。</p><p>映射规则：${esc(mapping)}。单批 ${fmt(c.candidate_batch_size, 0)} 个仅用于调度；模型调用并发 ${fmt(c.model_concurrency, 0)}；单模型最多 ${fmt(c.max_attempts, 0)} 次确定性尝试。</p><p class="research-meta">计数对账：发现 = 目标 + 排除；目标 = 待处理 + 运行中 + 完成 + 部分完成 + 失败。${unknown ? ` 当前页有 ${fmt(unknown, 0)} 个“投递状态不确定”调用，系统不会自动重发，防止重复计费。` : ''}</p>${recoveryScope}`;
    body.innerHTML = items.map((item) => {
      const result = item.debate_id ? `<button type="button" class="btn ghost product-result" data-product-run-id="${Number(c.id)}" data-product-debate-id="${Number(item.debate_id)}">查看结果</button>` : '-';
      const failure = productFailureSummary(item);
      const reason = item.status === 'excluded'
        ? `${item.scan_stage || '-'} / ${item.exclusion_reason || '-'}`
        : `${result}${failure ? `<br><span class="muted">${esc(failure)}</span>` : ''}`;
      return `<tr><td><b>${esc(item.symbol)}</b><br><span class="muted">${esc(item.name)} · ${esc(item.exchange)}<br>连续 ${esc(item.mapping_symbol)}</span></td><td>${item.batch_no == null ? '-' : fmt(item.batch_no, 0)}</td><td>${esc(productItemLabel[item.status] || item.status)}</td><td class="left">${productModelCell(item, 'opening', participants)}</td><td class="left">${productModelCell(item, 'rebuttal', participants)}</td><td class="left">${reason}</td></tr>`;
    }).join('') || '<tr><td colspan="6" class="empty">当前页没有匹配任务</td></tr>';
    const total = Number(payload.total || 0);
    const first = total ? state.productResearchPage * state.productResearchPageSize + 1 : 0;
    const last = Math.min(total, (state.productResearchPage + 1) * state.productResearchPageSize);
    if (page) page.textContent = total ? `${first}-${last} / ${total}` : '';
    if (prev) prev.disabled = state.productResearchPage <= 0;
    if (next) next.disabled = last >= total;
  }
  async function loadProductResearch() {
    const offset = state.productResearchPage * state.productResearchPageSize;
    try {
      const selectedRun = state.productResearchRunId;
      const selectedQuery = selectedRun ? `&run_id=${encodeURIComponent(selectedRun)}` : '';
      state.productResearch = await api(`/api/product-research?limit=${state.productResearchPageSize}&offset=${offset}${selectedQuery}`);
      if (!state.productResearch?.run && !selectedRun) {
        state.productResearch = await api(`/api/product-research/resumable?limit=${state.productResearchPageSize}&offset=${offset}`);
        if (state.productResearch?.run?.id) rememberProductResearchRun(state.productResearch.run.id);
      }
    } catch (e) {
      state.productResearch = null;
    }
    renderProductResearch();
  }

  // ---------- 权益曲线 ----------
  let chart;
  function initChart() {
    chart = echarts.init($('#equity-chart'), null, { renderer: 'canvas' });
    window.addEventListener('resize', () => chart.resize());
  }
  async function refreshChart() {
    // 时间范围由服务端相对最新快照计算：回放竞赛的时间轴与浏览器时间无关
    const rangeSec = { '1d': 86400, '1w': 7 * 86400, '1m': 30 * 86400, all: 0 }[state.range];
    const r = await api(`/api/equity?range_sec=${rangeSec}`);
    const init = r.initial_cash;
    const series = Object.entries(r.series).map(([tid, pts]) => ({
      name: state.names[tid] || tid, type: 'line', showSymbol: false, smooth: false, lineStyle: { width: 2 }, color: state.colors[tid],
      data: pts.map(([t, e]) => [t * 1000, state.pct ? +((e / init - 1) * 100).toFixed(3) : +e.toFixed(2)]),
    }));
    chart.setOption({
      backgroundColor: 'transparent', animation: false,
      tooltip: { trigger: 'axis', valueFormatter: (v) => state.pct ? pct(v) : fmt(v) },
      legend: { top: 0, textStyle: { color: '#c9d1d9' } },
      grid: { left: 70, right: 20, top: 36, bottom: 30 },
      xAxis: { type: 'time', axisLine: { lineStyle: { color: '#3a4552' } }, axisLabel: { color: '#8b98a8' } },
      yAxis: { type: 'value', scale: true, splitLine: { lineStyle: { color: '#26303c' } }, axisLabel: { color: '#8b98a8', formatter: (v) => state.pct ? v + '%' : fmt(v) } },
      series,
    }, { notMerge: true });
  }

  // ---------- 日汇总 ----------
  async function refreshDaily() {
    const r = await api('/api/daily?days=30');
    const days = [...new Set(r.items.map((i) => i.day))].sort().reverse();
    const traders = [...new Set(r.items.map((i) => i.trader_id))].sort();
    const idx = {}; r.items.forEach((i) => { idx[i.day + '|' + i.trader_id] = i; });
    $('#daily-tbl thead').innerHTML = '<tr><th>日期</th>' + traders.map((t) => `<th style="color:${state.colors[t] || ''}">${esc(state.names[t] || t)}</th>`).join('') + '</tr>';
    $('#daily-tbl tbody').innerHTML = days.map((day) => '<tr><td>' + day + '</td>' + traders.map((t) => {
      const i = idx[day + '|' + t]; if (!i) return '<td>-</td>';
      const p = i.start_equity ? (i.end_equity / i.start_equity - 1) * 100 : 0;
      return `<td class="${cls(i.pnl)}">${fmt(i.pnl)} <span class="muted">(${pct(p)}, ${i.trades}笔)</span></td>`;
    }).join('') + '</tr>').join('') || '<tr><td class="empty">暂无</td></tr>';
  }

  // ---------- 模型详情 ----------
  async function refreshDetail() {
    const d = state.data; if (!d || !state.trader) return;
    const t = d.traders.find((x) => x.id === state.trader); if (!t) return;
    const body = $('#detail-body');
    const pager = $('.pager');
    if (state.tab === 'positions') {
      pager.style.display = 'none';
      body.innerHTML = t.positions.length ? `<table class="tbl"><thead><tr><th>品种</th><th>方向</th><th>数量</th><th>均价</th><th>最新</th><th>浮动盈亏</th><th>名义价值</th><th>保证金</th><th>杠杆</th><th>止损</th><th>止盈</th><th>持仓时长</th></tr></thead><tbody>` +
        t.positions.map((p) => `<tr><td>${esc(p.symbol)} <span class="muted">${esc(p.name)}</span></td><td class="${p.side === 'long' ? 'pos' : 'neg'}">${p.side === 'long' ? '多' : '空'}</td><td>${p.qty}</td><td>${fmtPx(p.avg_price)}</td><td>${fmtPx(p.last)}</td><td class="${cls(p.unrealized)}">${fmt(p.unrealized)}</td><td>${fmt(p.notional)}</td><td>${fmt(p.margin)}</td><td>${p.leverage}x</td><td>${fmtPx(p.stop_loss)}</td><td>${fmtPx(p.take_profit)}</td><td>${holdText(p.hold_min)}</td></tr>`).join('') + '</tbody></table>' : '<div class="empty">当前无持仓</div>';
    } else if (state.tab === 'trades') {
      pager.style.display = '';
      const r = await api(`/api/trades?trader=${state.trader}&limit=${state.pageSize}&offset=${state.page * state.pageSize}`);
      $('#pg-info').textContent = `${state.page * state.pageSize + 1}-${Math.min((state.page + 1) * state.pageSize, r.total)} / ${r.total}`;
      body.innerHTML = r.items.length ? `<table class="tbl"><thead><tr><th>时间</th><th>轮次</th><th>品种</th><th>动作</th><th>数量</th><th>价格</th><th>手续费</th><th>已实现盈亏</th><th>备注</th></tr></thead><tbody>` +
        r.items.map((x) => `<tr><td>${tsShort(x.ts)}</td><td>${x.round_no}</td><td>${esc(x.symbol)} <span class="muted">${esc(r.names[x.symbol] || '')}</span></td><td>${actName[x.action] || x.action}</td><td>${x.qty}</td><td>${fmtPx(x.price)}</td><td>${fmt(x.fee, 2)}</td><td class="${cls(x.realized_pnl)}">${fmt(x.realized_pnl)}</td><td class="left muted">${esc(x.reason || '')}</td></tr>`).join('') + '</tbody></table>' : '<div class="empty">暂无成交</div>';
    } else {
      pager.style.display = '';
      const r = await api(`/api/decisions?trader=${state.trader}&limit=${state.pageSize}&offset=${state.page * state.pageSize}`);
      $('#pg-info').textContent = `第 ${state.page + 1} 页`;
      body.innerHTML = r.items.length ? r.items.map((x) => {
        const failure = decisionFailureMessage(x.failure_category);
        const acts = (x.actions || []).map((a, i) => {
          const res = (x.results || [])[i] || {};
          const ok = res.accepted;
          return `<div class="act"><span class="${ok ? 'ok' : 'rej'}">${ok ? '✔' : '✘'}</span> ${esc(a.symbol)} ${actName[a.action] || a.action}` +
            (a.size_pct ? ` ${(a.size_pct * 100).toFixed(0)}%` : '') + (a.stop_loss ? ` 止损 ${fmtPx(a.stop_loss)}` : '') + (a.take_profit ? ` 止盈 ${fmtPx(a.take_profit)}` : '') +
            (res.qty ? ` → 成交 ${res.qty}@${fmtPx(res.price)}` : '') + (!ok && res.reason ? ` <span class="rej">(${esc(res.reason)})</span>` : '') +
            (a.reason ? ` <span class="muted">${esc(a.reason)}</span>` : '') + '</div>';
        }).join('') || (failure
          ? '<div class="act rej">模型本轮失败，未形成有效决策</div>'
          : '<div class="act muted">无动作（观望）</div>');
        const tokenText = (x.tokens_in != null || x.tokens_out != null)
          ? `· tokens ${x.tokens_in ?? '-'}/${x.tokens_out ?? '-'}`
          : '';
        return `<div class="decision"><div class="head"><span>第 ${x.round_no} 轮 · ${tsShort(x.ts)} · ${x.mode}</span><span>${x.latency_ms ? x.latency_ms + 'ms' : ''} ${tokenText} <span class="link" data-dec="${x.id}">查看原文</span></span></div>` +
          (x.analysis ? `<div class="analysis">${esc(x.analysis)}</div>` : '') + acts + (failure ? `<div class="err">⚠ ${esc(failure)}</div>` : '') + '</div>';
      }).join('') : '<div class="empty">暂无决策</div>';
    }
  }
  const holdText = (m) => m == null ? '-' : m < 60 ? Math.round(m) + ' 分钟' : m < 1440 ? (m / 60).toFixed(1) + ' 小时' : (m / 1440).toFixed(1) + ' 天';

  async function refreshEvents() {
    const r = await api('/api/events?limit=60');
    $('#events-tbl tbody').innerHTML = r.items.map((e) => `<tr><td>${tsShort(e.ts)}</td><td class="${e.level === 'error' ? 'neg' : e.level === 'warn' ? 'pos' : ''}">${e.level}</td><td>${esc(e.source || '')}</td><td class="left">${esc(e.message)}</td></tr>`).join('');
  }

  // ---------- 赛前多空决策室（只读研究记录） ----------
  const stanceName = { bullish: '偏多', bearish: '偏空', neutral: '中性 / 分歧' };
  const debateStatusName = {
    running: '正在固定事实集', opening: '四方独立陈述中', rebuttal: '定向反证中',
    ready_for_codex_review: '等待 Codex 仲裁', incomplete: '研究未完整', unavailable: '证据或模型未就绪',
    aborted: '已中止', aborted_restarted: '因重启中止', failed: '研究失败',
  };

  function openModal(title, body, trigger) {
    state.modalTrigger = trigger || document.activeElement;
    $('#modal-title').textContent = title;
    $('#modal-body').textContent = body;
    $('#modal').classList.remove('hidden');
    requestAnimationFrame(() => $('#modal-close').focus());
  }

  function closeModal() {
    const modal = $('#modal');
    if (modal.classList.contains('hidden')) return;
    modal.classList.add('hidden');
    const trigger = state.modalTrigger;
    state.modalTrigger = null;
    if (trigger && document.contains(trigger)) trigger.focus();
  }

  function participant(d, traderId) {
    const found = (d?.traders || []).find((item) => item.id === traderId);
    return found || { id: traderId, name: traderId || '未命名模型', color: '#64748b' };
  }

  function stanceClass(stance) {
    return ['bullish', 'bearish', 'neutral'].includes(stance) ? stance : 'failed';
  }

  function factChips(factIds) {
    const ids = Array.isArray(factIds) ? factIds.filter((id) => typeof id === 'string') : [];
    if (!ids.length) return '<span class="research-meta">未引用可核验事实</span>';
    return `<div class="fact-chips">${ids.map((id) => `<button class="fact-chip" type="button" data-fact="${esc(id)}">${esc(id)}</button>`).join('')}</div>`;
  }

  function turnFor(turns, stage, traderId) {
    return (turns || []).find((turn) => turn.stage === stage && turn.trader_id === traderId) || null;
  }

  const debateFailureMessages = Object.freeze({
    timeout: '本轮请求在本地时限内未获得响应。',
    connection: '未能建立或保持与模型服务的连接。',
    authorization: '模型服务拒绝了此次认证或访问。',
    quota_or_credits: '该模型的可用额度已不足或用尽。',
    rate_limited: '模型服务限制了本次请求频率或可用额度。',
    upstream_4xx: '模型服务拒绝了本次请求。',
    upstream_5xx: '模型服务暂时无法处理本次请求。',
    malformed_json: '模型已返回内容，但没有形成可解析的 JSON 研究格式。',
    invalid_fact_citation: '模型已返回内容，但引用未通过本轮冻结事实集校验。',
    invalid_target: '模型已返回内容，但反证对象不符合本轮参与者规则。',
    missing_required_field: '模型已返回内容，但缺少研究结构中的必填字段。',
    invalid_schema: '模型已返回内容，但未通过本地研究结构校验。',
    invalid_response: '模型已返回内容，但未通过本地结构与事实引用校验。',
    stage_deadline: '本阶段在统一截止时间内未完成。',
    worker_failed: '本地请求工作线程未能返回结果。',
    participant_unavailable: '该参赛者当前不是可用的实时模型。',
    request_failed: '本次请求未完成，系统未保留可确认的具体原因。',
  });

  function debateFailureMessage(row, fallback) {
    const category = typeof row?.failure_category === 'string' ? row.failure_category : '';
    return debateFailureMessages[category] || fallback;
  }

  function debateParticipantIds(d, detail) {
    // 归档会话要按它当时的参与者渲染：交易员被移出配置后竞赛仍会续跑，
    // 用当前名单会让那位模型的开场与反证在历史里凭空消失，时间线还会把四条已完成回合判成未完成。
    const ids = [];
    for (const turn of (detail?.turns || [])) {
      if (turn && turn.trader_id && !ids.includes(turn.trader_id)) ids.push(turn.trader_id);
    }
    if (ids.length) return ids;
    return (d?.traders || []).map((trader) => trader.id);   // 还没有任何回合：用当前名单占位
  }

  function missingTurnState(detail, stage) {
    const status = typeof detail?.status === 'string' ? detail.status : '';
    const preliminary = typeof detail?.preliminary?.label === 'string' ? detail.preliminary.label : '';
    if (status === 'unavailable') {
      return {
        badge: '未调用',
        line: preliminary || '研究前置条件未满足，未调用模型。',
        note: '本轮没有向模型服务发送请求。',
      };
    }
    if (stage === 'rebuttal' && status === 'incomplete') {
      return {
        badge: '未进入',
        line: preliminary || '开场陈述未完整，未进入定向反证。',
        note: '本轮没有为该阶段发送模型请求。',
      };
    }
    if (['running', 'opening', 'rebuttal'].includes(status)) {
      const opening = stage === 'opening';
      return {
        badge: opening && status === 'running' ? '准备中' : '进行中',
        line: opening ? '统一事实集已创建，正在准备独立陈述。' : '正在等待开场陈述完整后进入定向反证。',
        note: '不会用其他模型的内容补造观点。',
      };
    }
    if (['incomplete', 'aborted', 'aborted_restarted', 'failed'].includes(status)) {
      return {
        badge: '已结束',
        line: preliminary || '本轮研究已结束，未形成该阶段的可回放材料。',
        note: '请查看本轮状态与固定失败分类。',
      };
    }
    return {
      badge: '尚未请求',
      line: '尚未创建本轮研究。',
      note: '不会用其他模型的内容补造观点。',
    };
  }

  function modelCards(d, detail) {
    const turns = detail?.turns || [];
    const ids = debateParticipantIds(d, detail);
    if (!ids.length) return '<div class="research-empty">等待参赛者配置。</div>';
    return ids.map((traderId) => {
      const meta = participant(d, traderId);
      const row = turnFor(turns, 'opening', traderId);
      const color = safeColor(meta.color);
      if (!row) {
        const missing = missingTurnState(detail, 'opening');
        return `<article class="debate-model" style="--participant-color:${color}"><div class="debate-model-head"><h4>${esc(meta.name)}</h4><span class="stance failed">${esc(missing.badge)}</span></div><p class="stance-line">${esc(missing.line)}</p><p class="research-meta">${esc(missing.note)}</p></article>`;
      }
      if (row.status !== 'complete') {
        const message = debateFailureMessage(row, '本模型未返回可验证的结构化研究结论。');
        return `<article class="debate-model" style="--participant-color:${color}"><div class="debate-model-head"><h4>${esc(meta.name)}</h4><span class="stance failed">未完成</span></div><p class="stance-line">${esc(message)}</p><p class="research-meta">本轮不会替代、重试或编造该模型观点。</p></article>`;
      }
      const confidence = row.confidence == null ? '置信度未提供' : `置信度 ${(Number(row.confidence) * 100).toFixed(0)}%`;
      return `<article class="debate-model" style="--participant-color:${color}"><div class="debate-model-head"><h4>${esc(meta.name)}</h4><span class="stance ${stanceClass(row.stance)}">${esc(stanceName[row.stance] || '未分类')}</span></div><p class="stance-line">${esc(row.thesis || '')}</p>${factChips(row.supporting_fact_ids)}<p class="research-meta">${confidence} · 反方点：${esc(row.counter_argument || '未提供')}</p><p class="research-meta">失效：${esc(row.invalidation || '未提供')}</p></article>`;
    }).join('');
  }

  function rebuttalCards(d, detail) {
    const turns = detail?.turns || [];
    const ids = debateParticipantIds(d, detail);
    if (!ids.length) return '<div class="research-empty">等待参赛者配置。</div>';
    return ids.map((traderId) => {
      const meta = participant(d, traderId);
      const row = turnFor(turns, 'rebuttal', traderId);
      const color = safeColor(meta.color);
      if (!row) {
        const missing = missingTurnState(detail, 'rebuttal');
        return `<article class="rebuttal-card" style="--participant-color:${color}"><div class="rebuttal-card-head"><h4>${esc(meta.name)}</h4><span class="stance failed">${esc(missing.badge)}</span></div><p class="stance-line">${esc(missing.line)}</p></article>`;
      }
      if (row.status !== 'complete') {
        const message = debateFailureMessage(row, '未取得可验证反证，本轮不生成替代性结论。');
        return `<article class="rebuttal-card" style="--participant-color:${color}"><div class="rebuttal-card-head"><h4>${esc(meta.name)}</h4><span class="stance failed">未完成</span></div><p class="stance-line">${esc(message)}</p></article>`;
      }
      const target = participant(d, row.target_trader_id);
      const confidence = row.confidence == null ? '置信度未提供' : `修订置信度 ${(Number(row.confidence) * 100).toFixed(0)}%`;
      return `<article class="rebuttal-card" style="--participant-color:${color}"><div class="rebuttal-card-head"><h4>${esc(meta.name)} → ${esc(target.name)}</h4><span class="stance ${stanceClass(row.stance)}">${esc(stanceName[row.stance] || '未分类')}</span></div><p class="stance-line">${esc(row.challenge || '')}</p>${factChips(row.challenge_fact_ids)}<p class="research-meta">${confidence} · 修订：${esc(row.revised_thesis || '未提供')}</p><p class="research-meta">失效：${esc(row.invalidation || '未提供')}</p></article>`;
    }).join('');
  }

  function renderDebateTimeline(room, detail) {
    const status = detail?.status || room?.latest?.status || 'idle';
    const turns = detail?.turns || [];
    const expected = Math.max(1, debateParticipantIds(state.data, detail).length || 4);
    const openingComplete = turns.filter((turn) => turn.stage === 'opening' && turn.status === 'complete').length === expected;
    const rebuttalComplete = turns.filter((turn) => turn.stage === 'rebuttal' && turn.status === 'complete').length === expected;
    const terminalProblem = ['incomplete', 'unavailable', 'aborted', 'aborted_restarted', 'failed'].includes(status);
    const steps = [
      ['统一事实集', '同一行情快照', Boolean(detail || room?.latest)],
      ['独立陈述', '四个模型分别作答', openingComplete],
      ['定向反证', '只挑战可核验论点', rebuttalComplete],
      ['Codex 仲裁', '人工复制仲裁包后审理', status === 'ready_for_codex_review'],
    ];
    $('#debate-timeline').innerHTML = steps.map(([title, note, done], index) => {
      let stateClass = done ? 'done' : '';
      if (!done && !terminalProblem) {
        const activeIndex = status === 'rebuttal' ? 2 : status === 'ready_for_codex_review' ? 3 : status === 'opening' || status === 'running' ? 1 : 0;
        if (index === activeIndex) stateClass = 'active';
      }
      if (!done && terminalProblem && index > 0) stateClass = 'warn';
      return `<div class="debate-step ${stateClass}" data-index="${index + 1}"><b>${title}</b><span>${note}</span></div>`;
    }).join('');
  }

  function renderDecisionRoom(d) {
    const room = d?.preopen_debate || null;
    const detail = state.debate;
    const latest = room?.latest || null;
    const status = detail?.status || latest?.status || 'idle';
    const statusText = debateStatusName[status] || '尚未生成研究';
    const statusEl = $('#debate-status');
    statusEl.textContent = statusText;
    statusEl.className = 'research-status ' + (status === 'ready_for_codex_review' ? 'ready' : ['running', 'opening', 'rebuttal'].includes(status) ? 'running' : ['incomplete', 'unavailable', 'aborted', 'aborted_restarted', 'failed'].includes(status) ? 'warn' : '');

    const trigger = $('#btn-debate');
    const retryTrigger = $('#btn-debate-retry');
    const canStart = Boolean(room?.manual_enabled) && !Boolean(room?.running) && !state.debateStarting;
    const retryEligible = Boolean(detail) && Number(detail?.id) === Number(latest?.id)
      && ['incomplete', 'unavailable'].includes(detail?.status || '');
    trigger.hidden = retryEligible;
    retryTrigger.hidden = !retryEligible;
    trigger.disabled = !canStart;
    trigger.textContent = state.debateStarting ? '正在创建研究…' : '生成本次研究';
    trigger.title = canStart ? '使用一份统一行情快照发起研究；最多八次模型调用，可能产生费用。竞赛暂停时也可安全使用，且不会开启决策。' : '引擎已停止或已有研究正在进行。';
    retryTrigger.disabled = !canStart;
    retryTrigger.textContent = state.debateStarting ? '正在重新生成研究…' : '重新获取全量行情并重跑';
    retryTrigger.title = canStart
      ? '上一轮研究未完整。将新建快照、重新扫描全部候选；只有覆盖完整才会调用模型。不会下单。'
      : '引擎已停止或已有研究正在进行。';

    const analysis = detail?.global_analysis || latest?.global_analysis || {};
    const universe = detail?.snapshot?.universe_research || null;
    const executionAnalysis = detail?.snapshot?.execution_pool_analysis || {};
    const fullUniverseScope = analysis.scope === 'full_universe_5m_technical_breadth';
    const regime = analysis.market_regime || 'unknown';
    const regimeText = { bullish: '整体偏多', bearish: '整体偏空', neutral: '整体中性', mixed: '多空分歧', unknown: '尚无判断' }[regime] || '尚无判断';
    const breadth = analysis.breadth || {};
    const asOf = detail?.market_as_of || latest?.market_as_of;
    const analysisId = detail?.analysis_id || latest?.analysis_id;
    const hash = detail?.snapshot_hash || latest?.snapshot_hash;
    const sourceHealth = detail?.snapshot?.source_health || {};
    const quoteOnly = sourceHealth.history_quality === 'quote_only';
    const qualityNotice = quoteOnly
      ? '<div class="research-status warn" role="alert"><b>数据质量限制：仅即时行情，不能判断趋势延续。</b><br>分钟与小时 K 线暂不可用；本轮只可评价当前报价，置信度固定为低。</div>'
      : '';
    const fullUniverseHtml = (() => {
      if (!fullUniverseScope) return '';
      const counts = universe?.counts || {};
      const packet = universe?.model_packet || {};
      const discovered = counts.discovered ?? analysis.candidate_denominator;
      const eligible = counts.eligible ?? analysis.eligible_candidates;
      const scanned = counts.scanned ?? analysis.scanned_candidates ?? analysis.coverage;
      const catalogueExcluded = counts.catalogue_excluded ?? analysis.catalogue_excluded;
      const runtimeExcluded = counts.runtime_excluded ?? analysis.runtime_excluded;
      const universeStatus = universe?.scope_status || analysis.scope_status || 'unavailable';
      const complete = universeStatus === 'complete';
      const universeHash = universe?.scan_manifest_hash || analysis.universe_manifest_hash || '';
      const selected = packet.selected_count;
      const executionCoverage = executionAnalysis.coverage ?? analysis.execution_pool_coverage ?? detail?.snapshot?.symbol_scores?.length;
      const countsText = `发现 ${fmt(discovered, 0)} · 目录通过 ${fmt(eligible, 0)} · 5m 已扫描 ${fmt(scanned, 0)} · 目录排除 ${fmt(catalogueExcluded, 0)} · 运行时排除 ${fmt(runtimeExcluded, 0)}`;
      const detailButton = universe?.items?.length
        ? '<button type="button" class="btn ghost universe-detail" data-universe-research="1">查看逐合约状态与排除原因</button>'
        : '';
      if (universeStatus === 'unavailable') {
        return `<div class="research-metrics"><span class="metric-chip failed">全量候选研究不可用</span><span class="metric-chip">执行池 ${fmt(executionCoverage, 0)} 个</span></div><p>本轮未调用模型，不形成全量多空结论。</p><p class="research-meta">全量扫描哈希 ${esc(String(universeHash).slice(0, 18) || '-')}…；事实快照 ${asOf ? esc(tsShort(asOf)) : '未提供'} · ${esc(analysisId)}</p>${detailButton}`;
      }
      if (!complete) {
        return `<div class="research-metrics"><span class="metric-chip failed">全量 eligible 覆盖未完成</span><span class="metric-chip">执行池 ${fmt(executionCoverage, 0)} 个</span></div><p>全量范围：${countsText}。</p><p>本轮未调用模型，不形成全量多空结论。</p><p>已扫描候选技术宽度（不构成全量结论）：偏多 ${fmt((universe?.technical_breadth || {}).bullish_contracts, 0)} · 偏空 ${fmt((universe?.technical_breadth || {}).bearish_contracts, 0)} · 中性 ${fmt((universe?.technical_breadth || {}).neutral_contracts, 0)}。</p><p class="research-meta">全量扫描哈希 ${esc(String(universeHash).slice(0, 18) || '-')}…；事实快照 ${asOf ? esc(tsShort(asOf)) : '未提供'} · ${esc(analysisId)}</p>${detailButton}`;
      }
      return `<div class="research-metrics"><span class="metric-chip ${stanceClass(regime)}">${esc(regimeText)}</span><span class="metric-chip">置信度 ${esc(analysis.confidence || 'low')}</span><span class="metric-chip">全量 eligible 覆盖完成</span><span class="metric-chip">执行池 ${fmt(executionCoverage, 0)} 个</span></div><p>全量范围：${countsText}。</p><p>全量 eligible 技术宽度：偏多 ${fmt(breadth.bullish_contracts, 0)} · 偏空 ${fmt(breadth.bearish_contracts, 0)} · 中性 ${fmt(breadth.neutral_contracts, 0)}。</p><p class="research-meta">全部已扫描候选都参与确定性宽度计算；模型只收到固定大小的汇总与代表性事实${selected == null ? '' : `（${fmt(selected, 0)} 条代表行）`}，不代表模型逐条阅读未列明合约。</p><p class="research-meta">执行边界：仅执行池内 ${fmt(executionCoverage, 0)} 个合约可进入模拟决策与撮合；其余候选仅参与本次研究。</p><p class="research-meta">全量扫描哈希 ${esc(String(universeHash).slice(0, 18) || '-')}…；事实快照 ${asOf ? esc(tsShort(asOf)) : '未提供'} · ${esc(analysisId)}</p>${detailButton}`;
    })();
    const executionOnlyHtml = `${qualityNotice}<div class="research-metrics"><span class="metric-chip ${stanceClass(regime)}">${esc(regimeText)}</span><span class="metric-chip">置信度 ${esc(analysis.confidence || 'low')}</span><span class="metric-chip">执行池 ${fmt(analysis.coverage, 0)} 个配置品种</span></div><p>广度：偏多 ${fmt(breadth.bullish_contracts, 0)} · 偏空 ${fmt(breadth.bearish_contracts, 0)} · 中性 ${fmt(breadth.neutral_contracts, 0)}。</p><p>事实快照：${asOf ? esc(tsShort(asOf)) : '未提供'} · <span class="muted">${esc(analysisId)}</span></p><p class="research-meta">哈希 ${esc(String(hash || '').slice(0, 18) || '-')}…；这是旧会话的执行池研究，不代表全量候选目录。</p>`;
    const globalHtml = analysisId ? (fullUniverseHtml || executionOnlyHtml) : '<div class="research-empty">尚无已归档的统一事实集。点击“生成本次研究”只会创建研究记录，不会下单。</div>';
    $('#debate-global').innerHTML = globalHtml;

    const preliminary = detail?.preliminary || latest?.preliminary || {};
    const referee = status === 'ready_for_codex_review'
      ? `<p><b>四方材料齐备，等待 Codex 仲裁。</b></p><p>${esc(preliminary.note || '仲裁只评估证据结构、分歧和证伪条件；不输出交易指令。')}</p><p class="research-meta">开场票数：偏多 ${fmt(preliminary.opening_votes?.bullish, 0)} · 偏空 ${fmt(preliminary.opening_votes?.bearish, 0)} · 中性 ${fmt(preliminary.opening_votes?.neutral, 0)}</p>${detail?.referee_packet ? '<button type="button" class="btn ghost copy-packet" data-copy-packet="1">复制 Codex 仲裁包</button>' : ''}`
      : '<p><b>尚未进入仲裁。</b></p><p>只有四份独立陈述和四份反证都完整时，才会生成可复制的仲裁包。研究材料不会接入执行层。</p>';
    $('#debate-referee').innerHTML = referee;

    renderDebateTimeline(room, detail);
    $('#debate-models').innerHTML = modelCards(d, detail);
    $('#debate-rebuttals').innerHTML = rebuttalCards(d, detail);
    const list = state.debateList || [];
    $('#debate-history').innerHTML = list.length ? list.map((item) => `<button type="button" data-debate-id="${Number(item.id)}" class="${Number(item.id) === Number(state.debateId) ? 'on' : ''}" title="${esc(debateStatusName[item.status] || item.status || '')}">${esc(item.phase_label || item.phase || '研究')} · ${esc(debateStatusName[item.status] || item.status || '未知')}</button>`).join('') : '<span class="research-empty">暂无历史记录</span>';

    if (detail) {
      $('#debate-source').textContent = fullUniverseScope && (universe?.scope_status || analysis.scope_status) === 'complete'
        ? `已加载 ${detail.phase_label || detail.phase || '研究'}：全量候选逐项状态与 5m 技术宽度已冻结；执行池保持独立，不包含账户、持仓、订单或原始模型文本。`
        : fullUniverseScope
          ? `已加载 ${detail.phase_label || detail.phase || '研究'}：全量候选研究覆盖未完成或不可用；未调用模型，未形成全量多空结论。`
        : quoteOnly
          ? `已加载 ${detail.phase_label || detail.phase || '研究'}：仅即时行情，不能判断趋势延续；不包含账户、持仓、订单或原始模型文本。`
          : `已加载 ${detail.phase_label || detail.phase || '研究'}：同一行情快照、双轮可回放材料；不包含账户、持仓、订单或原始模型文本。`;
    } else if (room?.next) {
      $('#debate-source').textContent = `${room.next.label}由交易日历判定；系统只在开盘前 ${Math.round(Number(room.next.lead_window_sec || 0) / 60)} 分钟内自动创建研究。`;
    } else {
      $('#debate-source').textContent = room?.automatic_supported ? '等待下一次交易日历支持的开盘前窗口。' : '当前市场模式仅支持手动研究；不会在浏览器刷新时调用模型。';
    }
  }

  function showFact(factId, trigger) {
    const fact = (state.debate?.snapshot?.facts || []).find((item) => item && item.id === factId);
    if (!fact) {
      openModal('事实详情', '当前会话未加载该事实。请选择一条已归档研究记录后再查看。', trigger);
      return;
    }
    openModal(`事实详情 · ${factId}`, JSON.stringify(fact, null, 2), trigger);
  }

  async function refreshDebates() {
    const request = ++state.debateRequest;
    const competitionId = state.data?.competition?.id;
    if (!competitionId) return;
    try {
      const listing = await api('/api/debates?limit=12');
      if (request !== state.debateRequest || state.data?.competition?.id !== competitionId) return;
      state.debateList = listing.items || [];
      const latestId = state.data?.preopen_debate?.latest?.id;
      const selectedExists = state.debateList.some((item) => Number(item.id) === Number(state.debateId));
      if (!state.debateSelectedByUser || !selectedExists) state.debateId = latestId || state.debateList[0]?.id || null;
      if (!state.debateId) {
        state.debate = null;
        renderDecisionRoom(state.data);
        return;
      }
      const detail = await api(`/api/debates/${encodeURIComponent(state.debateId)}`);
      if (request !== state.debateRequest || state.data?.competition?.id !== competitionId) return;
      state.debate = detail;
      renderDecisionRoom(state.data);
    } catch (e) {
      if (request !== state.debateRequest) return;
      state.debate = null;
      renderDecisionRoom(state.data);
    }
  }

  // ---------- 事件绑定 ----------
  document.addEventListener('click', async (ev) => {
    if (ev.target.id === 'modal') { closeModal(); return; }
    const b = ev.target.closest('button, .link');
    if (!b) return;
    if (b.dataset.range) { state.range = b.dataset.range; $('#range-seg').querySelectorAll('[data-range]').forEach((x) => x.classList.toggle('on', x === b)); refreshChart(); }
    else if (b.id === 'pct-toggle') { state.pct = !state.pct; b.classList.toggle('on', state.pct); refreshChart(); }
    else if (b.dataset.debateId) {
      state.debateId = b.dataset.debateId;
      state.debateSelectedByUser = true;
      await refreshDebates();
    }
    else if (b.dataset.universeResearch) {
      const universe = state.debate?.snapshot?.universe_research;
      if (!universe) return;
      openModal('全量候选逐项状态', JSON.stringify({
        scope: universe.scope,
        scope_status: universe.scope_status,
        scan_manifest_hash: universe.scan_manifest_hash,
        catalogue: universe.catalogue,
        counts: universe.counts,
        coverage: universe.coverage,
        technical_breadth: universe.technical_breadth,
        exchange_breadth: universe.exchange_breadth,
        items: universe.items,
      }, null, 2), b);
    }
    else if (b.dataset.fact) { showFact(b.dataset.fact, b); }
    else if (b.dataset.copyPacket) {
      const packet = state.debate?.referee_packet;
      if (!packet) return;
      try {
        if (!navigator.clipboard?.writeText) throw new Error('Clipboard unavailable');
        await navigator.clipboard.writeText(packet);
        const original = b.textContent;
        b.textContent = '已复制仲裁包';
        setTimeout(() => { b.textContent = original; }, 1500);
      } catch (e) {
        openModal('Codex 仲裁包', packet, b);
      }
    }
    else if (b.id === 'btn-research-scan') {
      if (!confirm('将读取已校验候选目录并访问公开行情，生成本轮全量技术扫描。不会调用模型、不会下单、不会修改持仓。继续吗？')) return;
      state.researchScanStarting = true;
      renderResearchScan();
      try {
        await api('/api/research-scan/run', { method: 'POST' });
        await waitForResearchScan();
      } catch (e) {
        alert('未能启动全量扫描：' + (e.message || '请稍后重试'));
      } finally {
        state.researchScanStarting = false;
        await loadResearchScan();
      }
    }
    else if (b.id === 'btn-product-research') {
      const upper = Number(state.universe?.count || 0) * 8;
      const costNote = upper > 0
        ? `当前目录最多会产生 ${upper} 次模型调用（发现后会按实际可研究品种重算；不含确定性重试）。具体费用取决于四个已配置模型的计费口径。`
        : '将先动态刷新目录，再按实际可研究品种计算调用量。具体费用取决于四个已配置模型的计费口径。';
      if (!confirm(`将启动全品种赛前论证。${costNote}\n\n每个符合条件品种将独立执行四模型开场与反证；缺行情、停牌或不支持会记录具体原因。不会下单、不会修改持仓。继续吗？`)) return;
      state.productResearchStarting = true;
      renderProductResearch();
      try {
        const created = await api('/api/product-research/run', { method: 'POST' });
        if (created?.run_id) rememberProductResearchRun(created.run_id);
        state.productResearchPage = 0;
        await loadProductResearch();
      } catch (e) {
        alert('未能启动全品种论证：' + (e.message || '请稍后重试'));
      } finally {
        state.productResearchStarting = false;
        renderProductResearch();
      }
    }
    else if (b.id === 'btn-product-research-resume' || b.id === 'btn-product-research-retry') {
      const runId = Number(state.productResearch?.run?.id);
      if (!Number.isInteger(runId) || runId <= 0) return;
      const retrying = b.id === 'btn-product-research-retry';
      if (!confirm(retrying
        ? '只会补跑有明确失败回执的模型调用；已完成结果不会重发，投递状态不确定的调用不会自动重发。继续吗？'
        : '将续跑未完成任务；已完成结果不会重发，投递状态不确定的调用不会自动重发。继续吗？')) return;
      state.productResearchStarting = true;
      renderProductResearch();
      try {
        await api(`/api/product-research/${runId}/${retrying ? 'retry-failed' : 'resume'}`, { method: 'POST' });
        await loadProductResearch();
      } catch (e) {
        alert(`未能${retrying ? '补跑' : '续跑'}：` + (e.message || '请稍后重试'));
      } finally {
        state.productResearchStarting = false;
        renderProductResearch();
      }
    }
    else if (b.id === 'product-research-prev' || b.id === 'product-research-next') {
      if (b.id === 'product-research-prev' && state.productResearchPage > 0) state.productResearchPage -= 1;
      if (b.id === 'product-research-next') state.productResearchPage += 1;
      await loadProductResearch();
    }
    else if (b.dataset.productDebateId) {
      try {
        const runId = Number(b.dataset.productRunId);
        if (!Number.isInteger(runId) || runId <= 0) throw new Error('研究运行标识无效');
        const detail = await api(`/api/product-research/${encodeURIComponent(runId)}/debates/${encodeURIComponent(b.dataset.productDebateId)}`);
        openModal(`品种论证结果 · ${detail.phase_label || detail.phase || b.dataset.productDebateId}`,
          JSON.stringify({ id: detail.id, status: detail.status, snapshot: detail.snapshot, turns: detail.turns }, null, 2), b);
      } catch (e) {
        alert('未能读取品种论证结果：' + (e.message || '请稍后重试'));
      }
    }
    else if (b.id === 'btn-debate' || b.id === 'btn-debate-retry') {
      const retrying = b.id === 'btn-debate-retry';
      const retryId = Number(state.debate?.id);
      const legacyRetry = retrying && !String(state.debate?.analysis_id || '').startsWith('preopen-debate/v2:');
      const confirmation = retrying
        ? `将创建新的全量候选事实集并从头重新扫描全部候选。覆盖完整后，最多产生 8 次模型调用及相应费用；覆盖不完整时不会调用模型，也不会退回 8 个合约。旧研究会保留，不会下单、不会修改持仓或排行榜。${legacyRetry ? '\n\n当前记录是旧版执行池研究；本次将创建新的 v2 全量候选研究，而非续跑旧记录。' : ''}\n\n继续吗？`
        : '将以同一行情快照发起四个模型的两轮研究，最多产生 8 次模型调用及相应费用。它不会下单、不会修改持仓或排行榜。继续吗？';
      if (retrying && (!Number.isInteger(retryId) || retryId <= 0)) {
        alert('当前研究记录尚未加载完成，请稍后重试。');
        return;
      }
      if (!confirm(confirmation)) return;
      state.debateStarting = true;
      renderDecisionRoom(state.data);
      try {
        await api(retrying ? `/api/debates/${encodeURIComponent(retryId)}/retry` : '/api/debates/run', { method: 'POST' });
        state.debateSelectedByUser = false;
        await poll();
        await refreshDebates();
      } catch (e) {
        alert(`未能${retrying ? '重新生成' : '启动'}研究：` + (e.message || '请稍后重试'));
      } finally {
        state.debateStarting = false;
        renderDecisionRoom(state.data);
      }
    }
    else if (b.dataset.trader) { state.trader = b.dataset.trader; state.page = 0; renderCards(state.data); refreshDetail(); }
    else if (b.dataset.tab) { state.tab = b.dataset.tab; state.page = 0; $('#detail-tabs').querySelectorAll('button').forEach((x) => x.classList.toggle('on', x === b)); refreshDetail(); }
    else if (b.id === 'pg-prev') { if (state.page > 0) { state.page--; refreshDetail(); } }
    else if (b.id === 'pg-next') { state.page++; refreshDetail(); }
    else if (b.id === 'btn-pause') { await api('/api/control', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ action: state.data.status.paused ? 'resume' : 'pause' }) }); poll(); }
    else if (b.id === 'btn-round') { await api('/api/control', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ action: 'force_round' }) }); }
    else if (b.id === 'btn-reset') {
      if (confirm('确定重置竞赛？当前竞赛将归档，所有模型从起始资金重新开始。')) {
        try {
          await api('/api/control', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ action: 'reset' }) });
          state.page = 0; poll(); refreshChart(); refreshDaily();
        } catch (e) { alert('重置失败：' + e.message); }   // 例如触发成交尚未落库（409），稍后再试
      }
    }
    else if (b.dataset.dec) { const d = await api('/api/decisions/' + b.dataset.dec); openModal(`第 ${d.round_no} 轮 · ${state.names[d.trader_id] || d.trader_id}`, '=== 模型原始回复 ===\n' + (d.raw_response || '(空)') + '\n\n=== Prompt ===\n' + (d.prompt || '(规则策略无 prompt)'), b); }
    else if (b.id === 'modal-close') { closeModal(); }
  });

  document.addEventListener('keydown', (ev) => {
    const modal = $('#modal');
    if (modal.classList.contains('hidden')) return;
    if (ev.key === 'Escape') { ev.preventDefault(); closeModal(); }
    if (ev.key === 'Tab') {
      ev.preventDefault();
      $('#modal-close').focus();
    }
  });

  // ---------- 轮询 ----------
  // 以后端 data_version 判断是否有新数据：决策结算、成交、事件写入后才递增。
  // 轮次号在一轮开始时就变化，若据此刷新，模型结果晚于一个轮询周期落库时决策日志会滞后一轮。
  let lastVersion = null;
  async function poll() {
    const request = ++state.stateRequest;
    try {
      const d = await api('/api/state');
      if (request !== state.stateRequest) return;
      const changedCompetition = state.competitionId !== null && state.competitionId !== d.competition.id;
      state.competitionId = d.competition.id;
      if (changedCompetition) {
        state.debateRequest++;
        state.debate = null;
        state.debateList = [];
        state.debateId = null;
        state.debateSelectedByUser = false;
        state.productResearch = null;
        state.productResearchPage = 0;
        lastVersion = null;
      }
      state.data = d;
      renderTop(d); renderCards(d); renderQuotes(d);
      renderDecisionRoom(d);
      if (d.data_version !== lastVersion) { lastVersion = d.data_version; refreshDetail(); refreshEvents(); refreshDebates(); loadProductResearch(); }
      else if (state.tab === 'positions') refreshDetail();
    } catch (e) { $('#status-pill').textContent = '连接断开'; $('#status-pill').className = 'pill paused'; }
  }
  document.addEventListener('input', (ev) => {
    if (ev.target.id !== 'universe-filter') return;
    state.universeFilter = ev.target.value || '';
    renderUniverse();   // 只重排已缓存的目录，不再请求后端
  });

  initChart();
  loadUniverse();
  loadResearchScan();
  loadProductResearch();
  poll().then(() => { refreshChart(); refreshDaily(); });
  setInterval(poll, 5000);
  setInterval(refreshChart, 15000);
  setInterval(refreshDaily, 60000);
})();
