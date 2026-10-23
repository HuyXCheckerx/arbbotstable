(() => {
  'use strict';
  const $ = id => document.getElementById(id);
  const form = $('calculatorForm');
  const state = { quoteId: null, data: null, selected: null, fetching: false, pending: false, revision: 0, timer: null, recalcTimer: null, updating: false };
  const fmt = (value, places = 2) => value == null ? '—' : Number(value).toLocaleString('en-US', {minimumFractionDigits: places, maximumFractionDigits: places});
  const signed = (value, places = 2) => value == null ? 'Unknown' : `${Number(value) > 0 ? '+' : Number(value) < 0 ? '−' : ''}${fmt(Math.abs(Number(value)), places)}`;
  const profitClass = value => value == null ? '' : Number(value) >= 0 ? 'positive-text' : 'negative-text';
  const routeName = profile => profile.startsWith('direct') ? 'Direct · no affiliate' : `${profile.split('/')[0]} · 1 bp`;
  const duration = seconds => Number(seconds) >= 3600 ? `${fmt(Number(seconds) / 3600, 1)}h` : `${Math.floor(Number(seconds) / 60)}m ${Number(seconds) % 60}s`;
  const el = (tag, text, className) => { const node = document.createElement(tag); if (text != null) node.textContent = text; if (className) node.className = className; return node; };

  function assumptions() {
    return Object.fromEntries([...document.querySelectorAll('[data-assumption]')].map(input => [input.dataset.assumption, input.value]));
  }

  function error(message) {
    $('errorBox').textContent = message || '';
    $('errorBox').hidden = !message;
  }

  async function request(url, options) {
    const response = await fetch(url, options);
    const body = await response.json();
    if (!response.ok || !body.ok) throw new Error(body.error || 'Unable to calculate. Try again.');
    return body;
  }

  function invalidateAmount() {
    state.quoteId = null;
    state.revision++;
    $('results').hidden = true;
    $('emptyState').hidden = false;
    $('exportReport').disabled = true;
    clearTimeout(state.timer);
    state.timer = setTimeout(fetchQuotes, 700);
  }

  async function fetchQuotes() {
    if (!form.reportValidity()) return;
    if (state.fetching) { state.pending = true; return; }
    clearTimeout(state.timer);
    const amount = $('amount').value;
    state.fetching = true;
    state.quoteId = null;
    state.revision++;
    $('results').hidden = true;
    $('emptyState').hidden = true;
    $('loadingBox').hidden = false;
    $('refreshQuotes').disabled = true;
    $('refreshLabel').textContent = 'Fetching live quotes…';
    $('quoteStatus').textContent = 'Fetching public market data';
    $('statusDot').className = 'status-dot';
    error('');
    try {
      const data = await request(`/api/calculator/quotes?amount=${encodeURIComponent(amount)}`);
      if ($('amount').value !== amount) { state.pending = true; return; }
      state.quoteId = data.quote_id;
      state.data = data;
      state.selected = data.best_profile || data.routes[0]?.profile;
      await recalculate();
    } catch (err) {
      error(err.message);
      $('quoteStatus').textContent = 'Quote refresh unavailable';
      $('emptyState').hidden = false;
    } finally {
      state.fetching = false;
      $('loadingBox').hidden = true;
      $('refreshQuotes').disabled = false;
      $('refreshLabel').textContent = 'Refresh live quotes';
      if (state.pending) { state.pending = false; fetchQuotes(); }
    }
  }

  async function recalculate() {
    if (!state.quoteId || !form.checkValidity()) return;
    const revision = ++state.revision;
    const quoteId = state.quoteId;
    try {
      const data = await request('/api/calculator/calculate', {
        method: 'POST', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({quote_id: quoteId, assumptions: assumptions()})
      });
      if (revision !== state.revision || quoteId !== state.quoteId) return;
      state.data = data;
      state.updating = false;
      error('');
      render();
    } catch (err) {
      if (revision === state.revision) { error(err.message); $('results').classList.add('stale-results'); }
    }
  }

  function ledger(container, entries) {
    container.replaceChildren(...entries.map(item => {
      const row = el('div', null, item.total ? 'ledger-total' : item.separator ? 'ledger-separator' : '');
      if (item.separator) { row.textContent = item.label; return row; }
      row.append(el('dt', item.label));
      const value = el('dd', item.value, item.className);
      if (item.note) value.append(el('small', item.note));
      row.append(value);
      return row;
    }));
  }

  function render() {
    const data = state.data;
    if (!data?.routes?.length) return;
    const route = data.routes.find(r => r.profile === state.selected) || data.routes[0];
    state.selected = route.profile;
    const a = data.assumptions;
    const s = route.stress;
    const e = route.expected;
    const p = route.pool_model;
    $('assumptionSummary').textContent = `Conversion ${fmt(a.conversion_rate, 4)}× · sale at ${fmt(a.exit_rate, 4)} USDC · ${fmt(a.other_cost)} USDC exit cost budget. Expand to edit.`;
    $('emptyState').hidden = true;
    $('results').hidden = false;
    $('results').classList.remove('stale-results');
    $('exportReport').disabled = false;
    $('routeCount').textContent = `${data.routes.length} LIVE ROUTES`;
    $('selectedRoute').textContent = routeName(route.profile);
    $('quoteTime').textContent = new Date(data.observed_at_utc).toLocaleString(undefined, {hour: '2-digit', minute: '2-digit', second: '2-digit'}) + ' · your local time';

    const profit = s.profit_usdc;
    const target = Number(a.target_profit);
    let title, detail, kind = '';
    if (profit == null) {
      title = 'Gas cost is unavailable. Net profit is unknown.';
      detail = 'Enter a sender gas budget under Advanced assumptions or refresh live quotes.';
    } else if (Number(profit) >= target) {
      title = 'Full-fill estimate clears your profit target.';
      detail = 'This depends on your conversion and selling assumptions, complete delivery, and the entered costs.';
      kind = 'positive';
    } else if (Number(profit) > 0) {
      title = 'Positive estimate, below your profit target.';
      detail = `The ${fmt(a.haircut_bps, 1)} bps haircut scenario returns less than your ${fmt(target)} USDC target.`;
    } else {
      title = 'This full-fill estimate does not return a profit.';
      detail = 'The expected proceeds do not cover your starting capital and separate cost budgets.';
      kind = 'negative';
    }
    if (p && profit != null && Number(profit) > 0 && Number(p.profit_usdc) < 0) detail += ' The pool-capacity model also loses money.';
    $('verdict').className = `verdict ${kind}`;
    $('verdictIcon').textContent = kind === 'positive' ? '↗' : kind === 'negative' ? '↘' : '↔';
    $('verdictTitle').textContent = title;
    $('verdictDetail').textContent = detail;
    $('profitValue').textContent = signed(profit) + (profit == null ? '' : ' USDC');
    $('profitValue').className = profitClass(profit);
    $('profitSub').textContent = `${fmt(a.haircut_bps, 1)} bps output haircut · ${fmt(s.roi_bps, 2)} bps return`;
    $('breakEvenValue').textContent = fmt(s.break_even_exit, 6);
    $('poolProfitValue').textContent = p ? signed(p.profit_usdc) + (p.profit_usdc == null ? '' : ' USDC') : 'Unavailable';
    $('poolProfitValue').className = p ? profitClass(p.profit_usdc) : '';
    $('poolProfitSub').textContent = p ? `${fmt(p.fill_pct, 2)}% fills · ${fmt(p.filled_usdc, 2)} USDC swapped` : 'Pool model could not be calculated';

    $('routeRows').replaceChildren(...data.routes.map(r => {
      const row = el('tr', null, r.profile === route.profile ? 'selected' : '');
      const name = el('td');
      const button = el('button', null, 'route-button');
      button.type = 'button';
      button.setAttribute('aria-pressed', String(r.profile === route.profile));
      button.append(el('span', '', 'radio-mark'), el('span', routeName(r.profile)));
      button.addEventListener('click', () => { state.selected = r.profile; render(); });
      name.append(button);
      if (r.profile === data.best_profile) button.append(el('span', 'BEST', 'best-label'));
      row.append(name, el('td', fmt(r.expected.delivered_usdp, 4)), el('td', fmt(r.fees_usdp.total, 4)), el('td', signed(r.stress.profit_usdc), profitClass(r.stress.profit_usdc)), el('td', duration(r.estimated_seconds)));
      return row;
    }));
    $('routeErrors').hidden = !data.errors.length;
    $('routeErrors').textContent = data.errors.map(item => `${item.profile}: ${item.error}`).join(' · ');
    $('flowStart').textContent = fmt(data.amount_usdc);
    $('flowUsdp').textContent = fmt(e.delivered_usdp, 4);
    $('flowConverted').textContent = fmt(e.converted_tokens, 4);
    $('flowExit').textContent = fmt(e.sale_usdc, 4);
    $('profitEquation').textContent = `Expected full fill: ${fmt(e.sale_usdc, 4)} − ${fmt(data.amount_usdc, 4)} − ${fmt(e.costs_usdc, 4)} = ${signed(e.profit_usdc, 4)} USDC`;

    ledger($('feeLedger'), [
      {separator: true, label: 'ALREADY DEDUCTED FROM QUOTED USDP'},
      {label: 'Liquidity fees · both pool legs', value: `${fmt(route.fees_usdp.liquidity, 8)} USDP`},
      {label: 'Affiliate fee', value: `${fmt(route.fees_usdp.affiliate, 8)} USDP`},
      {label: 'Outbound delivery fee', value: `${fmt(route.fees_usdp.outbound, 8)} USDP`},
      {label: 'Embedded fee total', value: `${fmt(route.fees_usdp.total, 8)} USDP`, total: true},
      {separator: true, label: 'SEPARATE COSTS IN USDC'},
      {label: 'Sender deposit + approval gas', value: `${fmt(route.sender_gas_usdc, 4)} USDC`, note: a.gas_override == null ? 'Live gas price × assumed gas units' : 'Your manual override'},
      {label: 'Conversion + exit cost budget', value: `${fmt(a.other_cost, 4)} USDC`},
      {label: 'Extra service fee budget', value: `${fmt(a.service_cost, 4)} USDC`},
      {label: 'Separate costs used in profit', value: `${fmt(e.costs_usdc, 4)} USDC`, total: true}
    ]);
    const model = route.gas_model;
    $('gasDetail').textContent = a.gas_override == null ? `Gas budget: ${fmt(model.swap_gas_units_assumed, 0)} deposit + ${fmt(model.approval_gas_units_assumed, 0)} approval units × ${fmt(model.gas_price_gwei, 6)} gwei × ${fmt(model.eth_price_usdc)} USDC/ETH. Approval allowance is unknown; an approval budget is included.` : 'Sender gas is set by your override. Later transaction costs remain in the conversion + exit budget.';

    ledger($('targetLedger'), [
      {label: 'Expected full-fill profit', value: `${signed(e.profit_usdc, 4)} USDC`, className: profitClass(e.profit_usdc)},
      {label: 'Haircut scenario profit', value: `${signed(s.profit_usdc, 4)} USDC`, className: profitClass(s.profit_usdc)},
      {label: 'USDP after output haircut', value: `${fmt(s.delivered_usdp, 8)}`},
      {label: 'Your target', value: `${fmt(a.target_profit)} USDC`},
      {label: 'USDP delivery needed for target', value: fmt(route.target_required_usdp, 8), total: true},
      {label: 'Exit rate for target · expected fill', value: `${fmt(route.target_exit_rate, 8)} USDC/token`},
      {label: 'Estimated streaming sub-swaps', value: fmt(route.quantity, 0)}
    ]);

    $('fillPctLabel').textContent = `${fmt(a.fill_pct, 0)}%`;
    $('partialRows').replaceChildren(...route.scenarios.map(item => {
      const row = el('tr', null, Number(item.fill_pct) === Number(a.fill_pct) ? 'custom-row' : '');
      row.append(el('td', `${fmt(item.fill_pct, 0)}%`), el('td', fmt(item.filled_usdc, 2)), el('td', fmt(item.delivered_usdp, 4)), el('td', fmt(item.refund_usdc, 4)), el('td', signed(item.profit_usdc, 4), profitClass(item.profit_usdc)));
      return row;
    }));
    const poolStats = p ? [
      ['USDP pool reserve', `${fmt(data.market.usdp_depth)} USDP`],
      ['Marginal spot rate', `${fmt(data.market.spot_usdp_per_usdc, 6)}`, 'USDP / USDC · before fees'],
      ['Modeled filled capital', `${fmt(p.filled_usdc)} USDC`, `${fmt(p.fill_pct, 3)}% of deposit`],
      ['Modeled delivered output', `${fmt(p.delivered_usdp, 4)} USDP`],
      ['Modeled refund', `${fmt(p.refund_usdc, 4)} USDC`],
      ['Modeled net profit', `${signed(p.profit_usdc, 4)} USDC`],
      ['Inferred floor per leg', `${fmt(p.inferred_leg_floor_bps, 0)} bps`, 'Estimate from quote fees'],
      ['Memo floor', `+${fmt(a.floor_bps, 1)} bps`, `${fmt(Number(data.amount_usdc) * (1 + Number(a.floor_bps) / 10000), 4)} USDP target`]
    ] : [['Pool simulation', 'Unavailable', route.pool_model_error || 'No pool snapshot']];
    $('poolModelDetails').replaceChildren(...poolStats.map(([label, value, note]) => {
      const div = el('div'); div.append(el('span', label), el('strong', value)); if (note) div.append(el('small', note)); return div;
    }));
    updateExpiry();
  }

  function updateExpiry() {
    if (!state.data || $('results').hidden) return;
    const route = state.data.routes.find(r => r.profile === state.selected);
    if (!route) return;
    const expired = Number(route.expiry_unix) <= Date.now() / 1000;
    $('expiredNotice').hidden = !expired;
    $('results').classList.toggle('stale-results', expired || state.updating || !form.checkValidity());
    $('statusDot').className = `status-dot ${expired ? 'stale' : 'live'}`;
    const age = Math.max(0, Math.floor((Date.now() - Date.parse(state.data.observed_at_utc)) / 1000));
    $('quoteStatus').textContent = expired ? 'Quote expired · refresh needed' : `Live snapshot · ${age < 60 ? age + 's' : Math.floor(age / 60) + 'm'} ago`;
    if (expired) {
      $('verdictTitle').textContent = 'Quote expired. Refresh to assess current prices.';
      $('verdictDetail').textContent = 'The numbers below describe the previous snapshot only.';
      $('verdict').className = 'verdict';
      $('verdictIcon').textContent = '↔';
    }
  }

  form.addEventListener('submit', event => { event.preventDefault(); fetchQuotes(); });
  $('amount').addEventListener('input', () => {
    document.querySelectorAll('[data-amount]').forEach(button => button.classList.toggle('active', Number(button.dataset.amount) === Number($('amount').value)));
    invalidateAmount();
  });
  document.querySelectorAll('[data-amount]').forEach(button => button.addEventListener('click', () => {
    $('amount').value = button.dataset.amount;
    $('amount').dispatchEvent(new Event('input'));
  }));
  document.querySelectorAll('[data-assumption]').forEach(input => input.addEventListener('input', () => {
    state.revision++;
    state.updating = true;
    if (input.id === 'fillPct') $('fillPctLabel').textContent = `${input.value}%`;
    if (!form.checkValidity()) { error('Enter valid values in the assumption fields to update the calculation.'); $('results').classList.add('stale-results'); return; }
    $('results').classList.add('stale-results');
    clearTimeout(state.recalcTimer);
    state.recalcTimer = setTimeout(recalculate, 180);
  }));
  $('exportReport').addEventListener('click', () => {
    if (!state.data) return;
    const blob = new Blob([JSON.stringify({...state.data, selected_profile: state.selected, exported_at_utc: new Date().toISOString(), note: 'Indicative acquisition quotes; conversion and sale rates are assumptions. Partial fills and pool simulation are models.'}, null, 2)], {type:'application/json'});
    const url = URL.createObjectURL(blob);
    const link = el('a'); link.href = url; link.download = `usdp-calculation-${new Date().toISOString().slice(0,10)}.json`; link.click();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  });
  setInterval(updateExpiry, 1000);
  $('cycleAssumptions').open = window.matchMedia('(min-width: 851px)').matches;
  fetchQuotes();
})();
