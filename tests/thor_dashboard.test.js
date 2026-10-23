import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';

function client(fetch = async () => { throw new Error('unexpected fetch'); }) {
  const elements = new Map();
  const node = id => {
    if (!elements.has(id)) {
      const classes = new Set();
      elements.set(id, {
        id, textContent: '', innerHTML: '', className: '', hidden: false, disabled: false,
        value: '', style: {}, dataset: {}, parentElement: {style: {}},
        classList: {add: x => classes.add(x), remove: x => classes.delete(x), toggle: (x, on) => on ? classes.add(x) : classes.delete(x)},
        removeAttribute(k) { delete this[k]; }, setAttribute(k, v) { this[k] = v; },
        querySelector: selector => node(id + selector), addEventListener() {},
      });
    }
    return elements.get(id);
  };
  const context = vm.createContext({console, fetch, URLSearchParams, Date, Event, AbortController,
    setInterval() {}, setTimeout() {}, clearTimeout() {}, navigator: {},
    document: {getElementById: node, querySelector: node, querySelectorAll: () => [],
      addEventListener() {}, createElement: () => node('created')}});
  let source = fs.readFileSync(new URL('../src/web/static/thor_dashboard.js', import.meta.url), 'utf8');
  source = source.replace('  // Init', '  globalThis.testing = {state, fetchStatus, renderDepositInfo, renderEmptySwap, renderUsdtQuote, invalidateQuote, quoteFingerprint, renderTiming, requestUsdtQuote, invalidateOtherQuote, daemonPayload, updateDaemonModeControls};\n  // Init');
  vm.runInContext(source, context);
  return {node, ...context.testing};
}

const tx = '0x' + 'a'.repeat(64);

test('resting selection explains locked funds and disables the immediate profit threshold', () => {
  const c = client();
  c.node('sniperExecutionMode').value = 'resting';
  c.updateDaemonModeControls();
  assert.equal(c.node('sniperMinNetProfit').disabled, true);
  assert.equal(c.node('sniperMinNetProfitGroup').hidden, true);
  assert.match(c.node('sniperModeDescription').textContent, /locked until settlement/);
  assert.match(c.node('sniperModeDescription').textContent, /skips the immediate net-profit check/);
  assert.equal(c.daemonPayload().mode, 'resting');
  c.node('sniperExecutionMode').value = 'instant';
  c.node('sniperMinNetProfit').value = '2.5';
  c.updateDaemonModeControls();
  assert.equal(c.node('sniperMinNetProfit').disabled, false);
  assert.equal(c.node('sniperMinNetProfitGroup').hidden, false);
  assert.equal(c.daemonPayload().min_net_profit, '2.5');
  assert.match(c.node('sniperModeDescription').textContent, /after fees and gas/);
});

test('daemon payload preserves an explicitly selected zero-bps trigger', () => {
  const c = client();
  c.node('sniperExecutionMode').value = 'resting';
  c.node('sniperProfitBps').value = '0';
  c.node('sniperAmount').value = '100000';
  const payload = c.daemonPayload();
  assert.equal(payload.min_profit_bps, '0');
  assert.equal(payload.amount, '100000');
});

function info(status = 'COMPLETED_PARTIAL') {
  return {tx_id: tx, found: true, status, is_completed: status === 'COMPLETED_PARTIAL',
    progress_pct: 100, inbound: {amount: 10000, asset: 'ETH.USDC', memo: '=:ETH.USDP:wallet:1000200000000/20/720'},
    settlement: {out_usdp: 100, refund_usdc: 9899, swapped_usdc: 101},
    stages: status === 'COMPLETED_PARTIAL' ? {inbound_observed: {completed: true, final_count: 95},
      swap_finalised: {completed: true}, outbound_signed: {completed: true}} : {}};
}

test('switching from settled history to pending resets stage badges and stale memo', () => {
  const c = client();
  c.renderDepositInfo(info());
  assert.match(c.node('stageSwapFinalised').className, /completed/);
  const pending = info('PENDING');
  pending.inbound.memo = undefined;
  pending.progress_pct = 0;
  c.renderDepositInfo(pending);
  assert.equal(c.node('bannerStatusBadge').textContent, 'Awaiting observation');
  assert.equal(c.node('stageSwapFinalised').className, 'stage-step');
  assert.equal(c.node('streamProgressBar').style.width, '0%');
  assert.equal(c.node('inspectedMemoText').textContent, 'Unavailable');
  assert.equal(c.node('blocksProcessed').textContent, '—');
  assert.equal(c.node('settlementResult').hidden, true);
});

test('empty live queue removes completed ledger and timing', () => {
  const c = client();
  c.renderDepositInfo(info());
  c.renderEmptySwap({status: 'NO_ACTIVE_SWAP'});
  assert.equal(c.node('settlementCard').hidden, true);
  assert.equal(c.node('swapTiming').hidden, true);
  assert.equal(c.node('swapProgressPct').textContent, '—');
  assert.equal(c.node('bannerStatusBadge').textContent, 'NO ACTIVE SWAP');
});

test('a transient lookup failure preserves known stream details until a confirmed empty result', async () => {
  let deposit = {ok: false, found: false, status: 'UNAVAILABLE', error: 'THORNode timed out'};
  const c = client(async () => ({ok: true, json: async () => ({ok: true, timestamp: Date.now()/1000, deposit_info: deposit})}));
  const live = {...info('STREAMING'), streaming: {count: 3, quantity: 3306, swapped_usdc: 90.74, out_usdp: 90.86}};
  c.renderDepositInfo(live);
  await c.fetchStatus();
  assert.equal(c.node('bannerStatusBadge').textContent, 'Streaming');
  assert.equal(c.node('blocksProcessed').textContent, 3);
  assert.equal(c.node('streamMetricsGrid').hidden, false);
  assert.equal(c.node('swapSyncWarning').hidden, false);
  assert.match(c.node('swapSyncWarning').textContent, /last known swap details/);
  deposit = {ok: true, found: false, status: 'NO_ACTIVE_SWAP'};
  await c.fetchStatus();
  assert.equal(c.node('bannerStatusBadge').textContent, 'NO ACTIVE SWAP');
  assert.equal(c.node('swapSyncWarning').hidden, true);
  assert.equal(c.state.lastDepositInfo, null);
});

test('initial connection failure explains why swap information is unavailable', async () => {
  const c = client(async () => ({ok: false, status: 503}));
  await c.fetchStatus();
  assert.equal(c.node('bannerStatusBadge').textContent, 'UNAVAILABLE');
  assert.match(c.node('swapMessage').textContent, /running server/);
  assert.equal(c.node('swapSyncWarning').hidden, false);
});

test('temporarily missing slice details do not clear the last known stream', async () => {
  const c = client(async () => ({ok: true, json: async () => ({ok: true, timestamp: Date.now()/1000, deposit_info: info('STREAMING')})}));
  c.renderDepositInfo({...info('STREAMING'), streaming: {count: 3, quantity: 3306}});
  await c.fetchStatus();
  assert.equal(c.node('blocksProcessed').textContent, 3);
  assert.match(c.node('swapSyncWarning').textContent, /updated slice details/);
});

test('a failed lookup for another selection never retains the previous transaction', async () => {
  const c = client(async () => ({ok: false, status: 503}));
  c.renderDepositInfo(info('STREAMING'));
  c.state.customTx = '0x'+'b'.repeat(64);
  await c.fetchStatus();
  assert.equal(c.state.lastDepositInfo, null);
  assert.equal(c.node('streamMetricsGrid').hidden, true);
  assert.equal(c.node('bannerStatusBadge').textContent, 'UNAVAILABLE');
});

test('late status response from old selection cannot overwrite the current selection', async () => {
  const pending = [];
  const c = client(() => new Promise(resolve => pending.push(resolve)));
  const first = c.fetchStatus();
  c.state.customTx = tx;
  const second = c.fetchStatus();
  pending[1]({ok: true, json: async () => ({ok: true, timestamp: Date.now()/1000, deposit_info: info('PENDING')})});
  await second;
  pending[0]({ok: true, json: async () => ({ok: true, timestamp: Date.now()/1000, deposit_info: info()})});
  await first;
  assert.equal(c.node('bannerStatusBadge').textContent, 'Awaiting observation');
});

test('reverse quote labels output and fees in USDC', () => {
  const c = client();
  c.renderUsdtQuote({source: 'usdp', target: 'usdc', expected_output: 10001,
    profit_bps: 1, net_profit: 1, total_seconds: 60, pool_rate: 1.001, fees: {outbound: 2}});
  assert.equal(c.node('otherQuoteTitle').textContent, 'USDP → USDC quote');
  assert.match(c.node('usdtQuoteExpected').textContent, /USDC$/);
  assert.equal(c.node('usdtFeeOutbound').textContent, '2.00 USDC');
  assert.equal(c.node('usdtPoolRate').textContent, '1.00100 USDC/USDP');
});

test('changing any quote input removes executable stale quote', () => {
  const c = client();
  c.state.lastQuote = {input_amount: 10000};
  c.node('openSwapModalBtn').disabled = false;
  c.invalidateQuote();
  assert.equal(c.state.lastQuote, null);
  assert.equal(c.node('openSwapModalBtn').disabled, true);
  assert.equal(c.node('quoteResultCard').hidden, true);
});

test('countdown uses absolute finalization estimate and never invents a rapid-stream ETA', () => {
  const c = client();
  c.renderDepositInfo({...info('STREAMING'), timing: {estimated_finalize_at: Date.now()/1000 + 3600}});
  assert.match(c.node('swapTimeLeft').textContent, /1h|59m/);
  c.renderDepositInfo(info('STREAMING'));
  assert.equal(c.node('swapTimeLeft').textContent, 'Estimating…');
  assert.equal(c.node('swapFinalizeAt').textContent, 'Unavailable');
});


test('reverse quote displays unmet premium floor rather than implying a fill', () => {
  const c = client();
  c.renderUsdtQuote({source: 'usdp', target: 'usdc', expected_output: 9966,
    profit_bps: -34, net_profit: -34, total_seconds: 60, minimum_output: 10002,
    quote_meets_floor: false, floor_mode: '2bps', floor_warning: 'Slices may fail and refund fees can apply.'});
  assert.match(c.node('otherQuoteFloorStatus').textContent, /FLOOR NOT MET/);
  assert.equal(c.node('otherQuoteMinimum').textContent, '10,002.00 USDC');
});

test('an expired reverse quote is hidden and requires a new request', () => {
  const c = client();
  c.node('otherQuotePair').value = 'usdp';
  c.state.otherQuote = {expiry: Date.now()/1000 - 1};
  c.node('usdtQuoteResultCard').hidden = false;
  c.renderTiming();
  assert.equal(c.node('usdtQuoteResultCard').hidden, true);
  assert.match(c.node('otherQuoteMessage').textContent, /expired/);
  assert.equal(c.state.otherQuote, null);
});

test('input edits invalidate in-flight reverse quotes and release the button', async () => {
  let finish;
  const c = client(() => new Promise(resolve => { finish = resolve; }));
  c.node('otherQuotePair').value = 'usdp';
  c.node('otherQuoteSlippage').value = '50';
  const request = c.requestUsdtQuote();
  c.node('usdtQuoteAmount').value = '5000';
  c.invalidateOtherQuote();
  finish({json: async () => ({ok: true, source: 'usdp', target: 'usdc', expected_output: 9966,
    profit_bps: -34, net_profit: -34, total_seconds: 60})});
  await request;
  assert.equal(c.node('usdtQuoteResultCard').hidden, true);
  assert.equal(c.node('getUsdtQuoteBtn').disabled, false);
  assert.equal(c.state.otherQuote, null);
});


test('a partial fill shows the floor limitation and the realized loss after fees', () => {
  const c = client();
  const partial = info();
  partial.inbound.amount = 100000;
  partial.inbound.memo = '=:ETH.USDP:wallet:10002000000000/1/4164:symbiosis:1';
  partial.settlement = {out_usdp: 1252.54118923, refund_usdc: 98746.436028,
    swapped_usdc: 1253.3140516, effective_rate: .999383345};
  c.renderDepositInfo(partial);
  assert.equal(c.node('swapFloorNotice').hidden, false);
  assert.equal(c.node('settlementResult').hidden, false);
  assert.equal(c.node('settlementTokenDifference').textContent, '−1.02 at $1 parity');
  assert.match(c.node('settlementTokenDifference').className, /highlight-amber/);
  assert.doesNotMatch(c.node('settlementTokenDifference').className, /highlight-green/);
  assert.match(c.node('reconRateLabel').textContent, /after settlement fees/);
  c.renderEmptySwap({status: 'NO_ACTIVE_SWAP'});
  assert.equal(c.node('swapFloorNotice').hidden, true);
  assert.equal(c.node('settlementResult').hidden, true);
});

test('a positive quoted slice premium retains the wallet-return limitation', () => {
  const c = client();
  c.renderUsdtQuote({source: 'usdp', target: 'usdc', expected_output: 10010,
    profit_bps: 10, net_profit: 10, total_seconds: 60, minimum_output: 10002,
    quote_meets_floor: true, floor_mode: '2bps'});
  assert.equal(c.node('otherQuoteFloorNotice').hidden, false);
  assert.match(c.node('otherQuoteFloorStatus').textContent, /does not guarantee \+2 bps after settlement fees/);
  assert.match(c.node('otherQuoteFloorStatus').textContent, /full outbound fee still applies/);
  c.renderUsdtQuote({source: 'usdp', target: 'usdc', expected_output: 10010,
    profit_bps: 10, net_profit: 10, total_seconds: 60, quote_meets_floor: true, floor_mode: 'market'});
  assert.equal(c.node('otherQuoteFloorNotice').hidden, true);
});
