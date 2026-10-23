/**
 * THORChain USDC -> USDP Terminal Client Logic
 * Real-time polling, quoting, live execution, and comprehensive Deposit Tx Inspector.
 */

(function () {
  'use strict';

  const SLICE_FLOOR_FEE_NOTICE = 'The +2 bps setting sets the memo price target. Each slice is checked against the remaining target after prior fills. It does not guarantee +2 bps after settlement fees. If only a small portion fills, the full outbound fee still applies. Refund fees and wallet gas can leave the final return below your deposit.';

  // State
  let state = {
    pollingIntervalMs: 3000,
    pollTimer: null,
    isPolling: true,
    lastQuote: null,
    customTx: null, // Inspected deposit tx; null lets the server pick the wallet's newest
    latestTx: null, // Wallet's newest and previous deposits, from /api/latest_deposits
    previousTx: null,
    statusRequest: 0,
    lastDepositInfo: null,
    lastDepositSelection: null,
    timing: null,
    lastStatusAt: 0,
    clockOffset: 0,
    quoteRequest: 0,
    otherQuoteRequest: 0,
    otherQuote: null,
    pendingSwap: null, // Manual/resting swap process the server reports as still running
  };

  // DOM Elements
  const el = {
    operatorAddress: document.getElementById('operatorAddress'),
    operatorPill: document.getElementById('operatorPill'),
    copyAddressBtn: document.getElementById('copyAddressBtn'),
    refreshBtn: document.getElementById('refreshBtn'),
    refreshTimer: document.getElementById('refreshTimer'),

    // Top Stats
    usdcBalance: document.getElementById('usdcBalance'),
    usdcAllowance: document.getElementById('usdcAllowance'),
    usdpBalance: document.getElementById('usdpBalance'),
    ethBalance: document.getElementById('ethBalance'),
    swapStatusBadge: document.getElementById('swapStatusBadge'),
    swapProgressPct: document.getElementById('swapProgressPct'),
    swapBlockRatio: document.getElementById('swapBlockRatio'),
    swapTargetFloor: document.getElementById('swapTargetFloor'),
    poolSpotRate: document.getElementById('poolSpotRate'),
    poolUsdpDepth: document.getElementById('poolUsdpDepth'),

    // Deposit Transaction Inspector
    customTxInput: document.getElementById('customTxInput'),
    loadTxBtn: document.getElementById('loadTxBtn'),
    clearTxBtn: document.getElementById('clearTxBtn'),
    chipLatestSwap: document.getElementById('chipLatestSwap'),
    chipPreviousSwap: document.getElementById('chipPreviousSwap'),
    chipLiveQueue: document.getElementById('chipLiveQueue'),
    bannerTxLink: document.getElementById('bannerTxLink'),
    bannerStatusBadge: document.getElementById('bannerStatusBadge'),

    // Settlement Card
    settlementCard: document.getElementById('settlementCard'),
    settlementTypeBadge: document.getElementById('settlementTypeBadge'),
    settlementInbound: document.getElementById('settlementInbound'),
    settlementDelivered: document.getElementById('settlementDelivered'),
    settlementRefund: document.getElementById('settlementRefund'),
    settlementAffiliate: document.getElementById('settlementAffiliate'),
    settlementCollector: document.getElementById('settlementCollector'),
    outboundTxLink: document.getElementById('outboundTxLink'),
    refundTxLink: document.getElementById('refundTxLink'),
    reconSwapped: document.getElementById('reconSwapped'),
    reconRate: document.getElementById('reconRate'),
    reconTotalBack: document.getElementById('reconTotalBack'),

    // Streaming Metrics & Progress
    streamProgressContainer: document.getElementById('streamProgressContainer'),
    streamProgressLabel: document.getElementById('streamProgressLabel'),
    streamPercentText: document.getElementById('streamPercentText'),
    streamProgressBar: document.getElementById('streamProgressBar'),
    blocksProcessed: document.getElementById('blocksProcessed'),
    totalBlocks: document.getElementById('totalBlocks'),
    streamMetricsGrid: document.getElementById('streamMetricsGrid'),
    subswapsSuccess: document.getElementById('subswapsSuccess'),
    subswapsSuccessSub: document.getElementById('subswapsSuccessSub'),
    subswapsSkipped: document.getElementById('subswapsSkipped'),
    subswapsSkippedSub: document.getElementById('subswapsSkippedSub'),
    swappedCapital: document.getElementById('swappedCapital'),
    remainingCapital: document.getElementById('remainingCapital'),
    outboundAccumulated: document.getElementById('outboundAccumulated'),
    effectiveRateText: document.getElementById('effectiveRateText'),

    // Stages Flow
    stageInboundObserved: document.getElementById('stageInboundObserved'),
    stageInboundCounted: document.getElementById('stageInboundCounted'),
    stageInboundFinalised: document.getElementById('stageInboundFinalised'),
    stageSwapStatus: document.getElementById('stageSwapStatus'),
    stageSwapFinalised: document.getElementById('stageSwapFinalised'),
    stageObservedDetail: document.getElementById('stageObservedDetail'),
    stageStreamingDetail: document.getElementById('stageStreamingDetail'),
    stageOutboundDetail: document.getElementById('stageOutboundDetail'),

    // Telemetry & Memo
    telemetryStatus: document.getElementById('telemetryStatus'),
    latestReasonText: document.getElementById('latestReasonText'),
    inspectedMemoText: document.getElementById('inspectedMemoText'),

    // Quoter Tab
    quoteAmount: document.getElementById('quoteAmount'),
    quoteSlippage: document.getElementById('quoteSlippage'),
    quoteFloorMode: document.getElementById('quoteFloorMode'),
    quoteExecMode: document.getElementById('quoteExecMode'),
    quoteAffiliateName: document.getElementById('quoteAffiliateName'),
    quoteAffiliateBps: document.getElementById('quoteAffiliateBps'),
    verifyAffiliateBtn: document.getElementById('verifyAffiliateBtn'),
    affiliateLookupStatus: document.getElementById('affiliateLookupStatus'),
    getQuoteBtn: document.getElementById('getQuoteBtn'),
    openSwapModalBtn: document.getElementById('openSwapModalBtn'),
    maxUsdcHint: document.getElementById('maxUsdcHint'),
    chipBtns: document.querySelectorAll('#quoterTab .chip-btn'),

    // USDT -> USDP Quoter (quote only)
    usdtQuoteAmount: document.getElementById('usdtQuoteAmount'),
    usdtQuoteAffiliateName: document.getElementById('usdtQuoteAffiliateName'),
    usdtQuoteAffiliateBps: document.getElementById('usdtQuoteAffiliateBps'),
    usdtQuoteFloorMode: document.getElementById('usdtQuoteFloorMode'),
    getUsdtQuoteBtn: document.getElementById('getUsdtQuoteBtn'),
    usdtChipBtns: document.querySelectorAll('#usdtQuoterTab .chip-btn'),
    usdtQuoteProfitBadge: document.getElementById('usdtQuoteProfitBadge'),
    usdtQuoteExpected: document.getElementById('usdtQuoteExpected'),
    usdtQuoteNet: document.getElementById('usdtQuoteNet'),
    usdtQuoteStream: document.getElementById('usdtQuoteStream'),
    usdtPoolRate: document.getElementById('usdtPoolRate'),
    usdtFeeOutbound: document.getElementById('usdtFeeOutbound'),
    usdtFeeLiquidity: document.getElementById('usdtFeeLiquidity'),
    usdtFeeAffiliate: document.getElementById('usdtFeeAffiliate'),
    usdtMemoText: document.getElementById('usdtMemoText'),

    // Quote Result
    quoteResultCard: document.getElementById('quoteResultCard'),
    quoteProfitBadge: document.getElementById('quoteProfitBadge'),
    quoteExpectedUsdp: document.getElementById('quoteExpectedUsdp'),
    quoteNetProfit: document.getElementById('quoteNetProfit'),
    quoteStreamBlocks: document.getElementById('quoteStreamBlocks'),
    feeOutbound: document.getElementById('feeOutbound'),
    feeLiquidity: document.getElementById('feeLiquidity'),
    feeAffiliate: document.getElementById('feeAffiliate'),
    memoPreviewText: document.getElementById('memoPreviewText'),
    quoteRestingInfo: document.getElementById('quoteRestingInfo'),
    restingSlices: document.getElementById('restingSlices'),
    restingFillRate: document.getElementById('restingFillRate'),

    // Sniper Daemon Tab
    sniperDot: document.getElementById('sniperDot'),
    sniperStatusText: document.getElementById('sniperStatusText'),
    sniperPidBadge: document.getElementById('sniperPidBadge'),
    sniperAmount: document.getElementById('sniperAmount'),
    sniperFloorMode: document.getElementById('sniperFloorMode'),
    sniperProfitBps: document.getElementById('sniperProfitBps'),
    sniperPollSec: document.getElementById('sniperPollSec'),
    sniperAffiliateName: document.getElementById('sniperAffiliateName'),
    sniperAffiliateBps: document.getElementById('sniperAffiliateBps'),
    startSniperBtn: document.getElementById('startSniperBtn'),
    stopSniperBtn: document.getElementById('stopSniperBtn'),

    // Terminal
    terminalLogs: document.getElementById('terminalLogs'),
    autoscrollCheck: document.getElementById('autoscrollCheck'),
    clearLogsBtn: document.getElementById('clearLogsBtn'),

    // Modal
    swapModal: document.getElementById('swapModal'),
    closeModalBtn: document.getElementById('closeModalBtn'),
    cancelModalBtn: document.getElementById('cancelModalBtn'),
    submitLiveSwapBtn: document.getElementById('submitLiveSwapBtn'),
    modalConfirmText: document.getElementById('modalConfirmText'),
    modalInputAmount: document.getElementById('modalInputAmount'),
    modalMinOutput: document.getElementById('modalMinOutput'),
    modalSlippage: document.getElementById('modalSlippage'),
    modalRouter: document.getElementById('modalRouter'),
    modalOrderType: document.getElementById('modalOrderType'),

    // Tabs
    tabBtns: document.querySelectorAll('.tab-btn'),
    tabPanes: document.querySelectorAll('.tab-pane'),
  };

  // Helper formatting functions
  function formatMoney(num) {
    if (num === null || num === undefined || isNaN(num)) return '0.00';
    return Number(num).toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  }

  function formatShortAddress(addr) {
    if (!addr || addr.length < 12) return addr || '';
    return addr.slice(0, 6) + '...' + addr.slice(-4);
  }

  // --- API Calls ---
  function showSwapRefreshError(message) {
    document.getElementById('syncStatus').classList.add('sync-error');
    const cached = state.lastDepositInfo && state.lastDepositSelection === state.customTx;
    if (!cached) renderEmptySwap({status: 'UNAVAILABLE', message});
    const warning = document.getElementById('swapSyncWarning');
    if (warning) {
      warning.hidden = false;
      warning.textContent = cached ? `Refresh unavailable · showing last known swap details. ${message}` : message;
    }
  }

  async function fetchStatus() {
    const request = ++state.statusRequest;
    const selected = state.customTx;
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), 20000);
    try {
      let url = '/api/status';
      if (state.customTx) {
        url += '?tx=' + encodeURIComponent(state.customTx);
      }
      const res = await fetch(url, {signal: controller.signal, cache: 'no-store'});
      if (!res.ok) throw new Error(`Status request failed (${res.status})`);
      const data = await res.json();
      if (request !== state.statusRequest || selected !== state.customTx) return;
      state.lastStatusAt = Date.now();
      state.clockOffset = data.timestamp * 1000 - Date.now();
      document.getElementById('syncStatus').textContent = `Updated ${new Date().toLocaleTimeString()} · refresh after each poll`;
      document.getElementById('syncStatus').classList.toggle('sync-error', !data.ok || Boolean(data.deposit_info?.error));

      updateWallet(data.wallet);
      updatePools(data.pools);
      updateSniperStatus(data.sniper);

      const info = data.deposit_info;
      const cached = state.lastDepositInfo && state.lastDepositSelection === selected;
      const missingStream = cached && info?.found && info.status === 'STREAMING'
        && !info.streaming && state.lastDepositInfo.streaming
        && info.tx_id === state.lastDepositInfo.tx_id;
      if (missingStream) {
        showSwapRefreshError('The network has not returned updated slice details yet. Retrying automatically.');
      } else if (info?.found) {
        renderDepositInfo(info);
        if (info.error) showSwapRefreshError(info.error);
      } else if (!data.ok || info?.error || (cached && ['NOT_FOUND', 'UNAVAILABLE'].includes(info?.status))) {
        showSwapRefreshError(info?.error || info?.message || data.error || 'Swap lookup is temporarily unavailable. Retrying automatically.');
      } else {
        renderEmptySwap(info || {});
      }

      // Sync affiliate defaults
      if (data.affiliate && data.affiliate.name) {
        if (el.quoteAffiliateName && (!el.quoteAffiliateName.value || el.quoteAffiliateName.value === 'symbiosis')) {
          el.quoteAffiliateName.value = data.affiliate.name;
        }
        if (el.sniperAffiliateName && (!el.sniperAffiliateName.value || el.sniperAffiliateName.value === 'symbiosis')) {
          el.sniperAffiliateName.value = data.affiliate.name;
        }
      }
    } catch (err) {
      if (request !== state.statusRequest || selected !== state.customTx) return;
      document.getElementById('syncStatus').textContent = 'Connection interrupted · displayed data may be stale';
      document.getElementById('syncStatus').classList.add('sync-error');
      showSwapRefreshError('Cannot refresh from this webapp address. Check that it matches your running server, then refresh the page.');
      console.warn('Status poll error:', err);
    } finally {
      clearTimeout(timeout);
    }
  }

  async function fetchLogs() {
    try {
      const res = await fetch('/api/logs');
      if (!res.ok) return;
      const data = await res.json();
      if (!data.ok || !Array.isArray(data.logs)) return;

      renderLogs(data.logs);
    } catch (err) {
      console.warn('Logs poll error:', err);
    }
  }

  async function requestQuote() {
    const request = ++state.quoteRequest;
    const fingerprint = quoteFingerprint();
    state.lastQuote = null;
    el.openSwapModalBtn.disabled = true;
    el.quoteResultCard.hidden = true;
    const amount = el.quoteAmount.value || '10000';
    const slippage = el.quoteSlippage.value || '1';
    const affiliate = el.quoteAffiliateName ? el.quoteAffiliateName.value.trim() : 'symbiosis';
    const affiliateBps = el.quoteAffiliateBps ? el.quoteAffiliateBps.value.trim() : '1';
    const floorMode = el.quoteFloorMode ? el.quoteFloorMode.value : '2bps';
    const execMode = el.quoteExecMode ? el.quoteExecMode.value : 'instant';

    el.getQuoteBtn.disabled = true;
    el.getQuoteBtn.innerHTML = '<span>Quoting THORNode...</span>';

    try {
      const params = new URLSearchParams({
        amount: amount,
        slippage_bps: slippage,
        affiliate: affiliate,
        affiliate_bps: affiliateBps,
        floor_mode: floorMode,
        mode: execMode,
      });
      const res = await fetch(`/api/quote?${params.toString()}`);
      const data = await res.json();
      if (request !== state.quoteRequest || fingerprint !== quoteFingerprint()) return;
      if (!data.ok) {
        quoteMessage('Quote unavailable: ' + (data.error || 'Unknown error'));
        return;
      }
      state.lastQuote = {...data, fingerprint};
      el.openSwapModalBtn.disabled = false;
      quoteMessage(`Quoted ${new Date().toLocaleTimeString()} · full-fill market estimate; wallet gas excluded.${data.quote_meets_floor === false ? ' The market estimate is below the selected slice price floor.' : ''}`);
      renderQuote(data);
    } catch (err) {
      if (request === state.quoteRequest) quoteMessage('Quote request failed: ' + err.message);
    } finally {
      if (request !== state.quoteRequest) return;
      el.getQuoteBtn.disabled = false;
      el.getQuoteBtn.innerHTML = '<span>Get Live Quote</span>';
    }
  }

  function quoteFingerprint() {
    return [el.quoteAmount, el.quoteSlippage, el.quoteAffiliateName, el.quoteAffiliateBps, el.quoteFloorMode, el.quoteExecMode].map(node => node.value).join('|');
  }

  function quoteMessage(message) {
    document.getElementById('quoteMessage').textContent = message;
  }

  function invalidateQuote() {
    state.lastQuote = null;
    el.openSwapModalBtn.disabled = true;
    el.quoteResultCard.hidden = true;
    quoteMessage('Inputs changed · get a fresh quote to review this swap.');
  }

  async function requestUsdtQuote() {
    const source = document.getElementById('otherQuotePair').value;
    const target = source === 'usdp' ? 'USDC' : 'USDP';
    const request = ++state.otherQuoteRequest;
    const fingerprint = otherQuoteFingerprint();
    state.otherQuote = null;
    document.getElementById('otherQuoteMessage').textContent = 'Requesting a live quote…';
    el.getUsdtQuoteBtn.disabled = true;
    el.getUsdtQuoteBtn.textContent = 'Quoting THORNode…';
    document.getElementById('usdtQuoteResultCard').hidden = true;
    try {
      const params = new URLSearchParams({source, amount: el.usdtQuoteAmount.value || '10000', slippage_bps: document.getElementById('otherQuoteSlippage').value || '50',
        affiliate: el.usdtQuoteAffiliateName.value.trim(), affiliate_bps: el.usdtQuoteAffiliateBps.value.trim() || '0',
        floor_mode: el.usdtQuoteFloorMode.value});
      const res = await fetch(`/api/quote?${params}`);
      const data = await res.json();
      if (request !== state.otherQuoteRequest || fingerprint !== otherQuoteFingerprint()) return;
      if (!data.ok) throw new Error(data.error || 'Quote unavailable');
      state.otherQuote = {...data, fingerprint};
      renderUsdtQuote(data);
      document.getElementById('otherQuoteMessage').textContent = `Quoted ${new Date().toLocaleTimeString()} · expires ${formatTimestamp(Number(data.expiry))} · quote only`;
    } catch (err) {
      if (request === state.otherQuoteRequest) document.getElementById('otherQuoteMessage').textContent = err.message;
    } finally {
      if (request === state.otherQuoteRequest) {
        el.getUsdtQuoteBtn.disabled = false;
        el.getUsdtQuoteBtn.textContent = `Get ${source.toUpperCase()} → ${target} quote`;
      }
    }
  }

  function invalidateOtherQuote(message = 'Inputs changed · request a fresh quote.') {
    ++state.otherQuoteRequest;
    state.otherQuote = null;
    document.getElementById('usdtQuoteResultCard').hidden = true;
    document.getElementById('otherQuoteMessage').textContent = message;
    const source = document.getElementById('otherQuotePair').value;
    const target = source === 'usdp' ? 'USDC' : 'USDP';
    el.getUsdtQuoteBtn.disabled = false;
    el.getUsdtQuoteBtn.textContent = `Get ${source.toUpperCase()} → ${target} quote`;
  }

  function otherQuoteFingerprint() {
    return [document.getElementById('otherQuotePair'), el.usdtQuoteAmount, el.usdtQuoteAffiliateName, el.usdtQuoteAffiliateBps, el.usdtQuoteFloorMode, document.getElementById('otherQuoteSlippage')].map(node => node.value).join('|');
  }

  function renderUsdtQuote(q) {
    const target = (q.target || 'usdp').toUpperCase(), source = q.source.toUpperCase();
    document.getElementById('usdtQuoteResultCard').hidden = false;
    document.getElementById('otherQuoteTitle').textContent = `${source} → ${target} quote`;
    document.getElementById('otherQuoteOutputLabel').textContent = `Market estimate (${target})`;
    const bps = Number(q.profit_bps), positive = q.net_profit > 0;
    el.usdtQuoteProfitBadge.className = `profit-badge ${positive ? 'badge-positive' : 'badge-negative'}`;
    el.usdtQuoteProfitBadge.textContent = `${positive ? '+' : ''}${bps.toFixed(1)} bps`;
    el.usdtQuoteExpected.textContent = `${formatMoney(q.expected_output)} ${target}`;
    el.usdtQuoteNet.textContent = `${positive ? '+' : '-'}${formatMoney(Math.abs(q.net_profit))} at parity`;
    el.usdtQuoteStream.textContent = formatDuration(q.total_seconds);
    el.usdtPoolRate.textContent = q.pool_rate ? `${Number(q.pool_rate).toFixed(5)} ${target}/${source}` : 'Unavailable';
    const fees = q.fees || {};
    el.usdtFeeOutbound.textContent = `${formatMoney(fees.outbound || 0)} ${target}`;
    el.usdtFeeLiquidity.textContent = `${formatMoney(fees.liquidity || 0)} ${target}`;
    el.usdtFeeAffiliate.textContent = `${formatMoney(fees.affiliate || 0)} ${target}`;
    el.usdtMemoText.textContent = q.memo || '—';
    document.getElementById('otherQuoteMinimum').textContent = q.minimum_output == null ? 'Unavailable' : `${formatMoney(q.minimum_output)} ${target}`;
    const floorStatus = document.getElementById('otherQuoteFloorStatus');
    document.getElementById('otherQuoteFloorNotice').hidden = q.floor_mode === 'market';
    floorStatus.textContent = q.quote_meets_floor === false ? `FLOOR NOT MET · ${q.floor_warning}`
      : q.floor_mode === 'market' ? 'Market quote · minimum follows your slippage tolerance. Expected output includes quoted fees; wallet gas is excluded.'
      : SLICE_FLOOR_FEE_NOTICE;
    floorStatus.className = `panel-note ${q.quote_meets_floor === false ? 'highlight-amber' : ''}`;
  }

  // --- Update Top Stats UI ---
  function updateWallet(wallet) {
    if (!wallet) return;
    if (wallet.operator) {
      el.operatorAddress.textContent = formatShortAddress(wallet.operator);
      el.operatorPill.dataset.address = wallet.operator;
    }
    if (!wallet.rpc_connected) {
      [el.usdcBalance, el.usdpBalance, el.usdcAllowance, el.ethBalance].forEach(node => node.textContent = 'Unavailable');
      el.maxUsdcHint.textContent = 'Wallet RPC unavailable';
      return;
    }
    el.usdcBalance.innerHTML = `${formatMoney(wallet.usdc_balance)} <span class="unit">USDC</span>`;
    el.usdpBalance.innerHTML = `${Number(wallet.usdp_balance).toFixed(4)} <span class="unit">USDP</span>`;
    el.usdcAllowance.textContent = `${formatMoney(wallet.usdc_allowance)} USDC`;
    el.ethBalance.textContent = `${wallet.eth_balance} ETH`;
    el.maxUsdcHint.textContent = `Available: ${formatMoney(wallet.usdc_balance)} USDC`;
  }

  function updatePools(pools) {
    if (!pools) return;
    el.poolSpotRate.textContent = 'Unavailable';
    el.poolUsdpDepth.textContent = 'Unavailable';
    if (pools.spot_rate > 0) {
      el.poolSpotRate.innerHTML = `${Number(pools.spot_rate).toFixed(5)} <span class="unit">USDP/USDC</span>`;
    }
    if (pools.usdp_depth > 0) {
      el.poolUsdpDepth.textContent = `${formatMoney(pools.usdp_depth)} USDP`;
    }
  }

  function formatDuration(seconds) {
    if (seconds === null || seconds === undefined) return 'Estimating…';
    const value = Math.max(0, Math.ceil(seconds));
    if (value === 0) return 'Awaiting network';
    const hours = Math.floor(value / 3600), mins = Math.floor((value % 3600) / 60);
    return hours ? `${hours}h ${mins}m` : mins ? `${mins}m ${value % 60}s` : `${value}s`;
  }

  function formatTimestamp(seconds) {
    return seconds ? new Date(seconds * 1000).toLocaleString([], {
      month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit', second: '2-digit', timeZoneName: 'short'
    }) : 'Unavailable';
  }

  function renderTiming() {
    if (state.otherQuote?.expiry && Number(state.otherQuote.expiry) * 1000 <= Date.now()) invalidateOtherQuote('Quote expired · request a fresh quote.');
    if (state.lastQuote?.expiry && Number(state.lastQuote.expiry) * 1000 <= Date.now()) invalidateQuote();
    if (state.lastStatusAt && Date.now() - state.lastStatusAt > 30000) {
      document.getElementById('syncStatus').textContent = 'Last update over 30s ago · displayed data may be stale';
      document.getElementById('syncStatus').classList.add('sync-error');
    }
    const t = state.timing;
    if (!t) return;
    const now = (Date.now() + state.clockOffset) / 1000;
    document.getElementById('swapTimeLeft').textContent = t.finalized ? 'Finalized'
      : t.estimated_finalize_at ? formatDuration(t.estimated_finalize_at - now)
      : t.status === 'OUTBOUND_PENDING' ? 'Finalized' : 'Estimating…';
    document.getElementById('finalizeTimeLabel').textContent = t.finalized_at ? 'FINALIZED AT' : 'EST. FINALIZATION';
    document.getElementById('swapFinalizeAt').textContent = formatTimestamp(t.finalized_at || t.estimated_finalize_at);
    document.getElementById('swapStartAt').textContent = t.started_at ? `Started ${formatTimestamp(t.started_at)}` : 'Local timezone · network data';
    document.getElementById('swapTimeNote').textContent = t.finalized ? 'Stream ended · see wallet settlement below'
      : t.estimated_finalize_at ? (t.estimate_basis === 'observed_pace' ? 'Estimate · recent execution pace' : 'Estimate · assumes 6-second blocks')
      : 'Network has not provided a reliable estimate';
    document.getElementById('swapDeliveryAt').textContent = t.completed ? 'Outbound observed'
      : t.estimated_delivery_at ? `After ${formatTimestamp(t.estimated_delivery_at)}` : 'After finalization';
  }

  function resetStages() {
    [el.stageInboundObserved, el.stageInboundCounted, el.stageInboundFinalised, el.stageSwapStatus, el.stageSwapFinalised].forEach((stage) => {
      stage.className = 'stage-step';
      stage.querySelector('.stage-circle').textContent = '·';
    });
    el.stageObservedDetail.textContent = 'Awaiting observation';
    el.stageStreamingDetail.textContent = 'Awaiting execution';
    el.stageOutboundDetail.textContent = 'Awaiting delivery';
  }

  function renderEmptySwap(info = {}) {
    state.lastDepositInfo = null;
    state.lastDepositSelection = null;
    const warning = document.getElementById('swapSyncWarning');
    if (warning) warning.hidden = true;
    state.timing = null;
    const waiting = state.pendingSwap?.state === 'waiting';
    const title = state.customTx ? 'Inspecting transaction' : 'Current swap';
    document.getElementById('swapViewTitle').textContent = title;
    document.getElementById('swapViewSubtitle').textContent = state.customTx ? 'Manual inspection · use Current to follow wallet activity.' : 'Automatically follows active deposits from your wallet.';
    document.getElementById('swapMessage').textContent = info.error || (waiting ? 'Resting order is waiting for its price condition. No deposit has been sent.' : info.message) || 'No active swap found. Use a recent deposit to review its settlement.';
    el.bannerTxLink.textContent = state.customTx ? formatShortAddress(state.customTx) : 'No current deposit';
    el.bannerTxLink.removeAttribute('href');
    el.bannerStatusBadge.textContent = waiting ? 'WAITING FOR PRICE' : info.status === 'NO_ACTIVE_SWAP' ? 'NO ACTIVE SWAP' : info.status || 'WAITING';
    el.bannerStatusBadge.className = 'badge badge-subtle';
    el.swapStatusBadge.textContent = el.bannerStatusBadge.textContent;
    el.swapStatusBadge.className = 'stat-badge badge-amber';
    el.swapProgressPct.textContent = '—';
    el.swapTargetFloor.textContent = '—';
    [el.settlementCard, el.streamProgressContainer, el.streamMetricsGrid].forEach(node => node.hidden = true);
    document.getElementById('swapTiming').hidden = true;
    document.getElementById('swapFloorNotice').hidden = true;
    document.getElementById('settlementResult').hidden = true;
    document.querySelector('.stages-flow').hidden = true;
    document.querySelector('.telemetry-box').hidden = true;
    resetStages();
  }

  // Render every field on every selection, so history cannot leak into a live swap.
  function renderDepositInfo(info) {
    state.lastDepositInfo = info;
    state.lastDepositSelection = state.customTx;
    const warning = document.getElementById('swapSyncWarning');
    if (warning) warning.hidden = true;
    const txId = info.tx_id || info.inbound?.tx_hash || '';
    const status = info.status || 'PENDING';
    const settled = Boolean(info.is_completed);
    const finalized = Boolean(info.stages?.swap_finalised?.completed);
    const labels = {
      STREAMING: 'Streaming', LIMIT_WAITING: 'Waiting for price', INBOUND_CONFIRMING: 'Confirming deposit', PENDING: 'Awaiting observation',
      OUTBOUND_PENDING: 'Awaiting wallet delivery', COMPLETED: 'Completed',
      COMPLETED_PARTIAL: 'Partial fill + refund', REFUNDED: 'Refunded'
    };
    const label = labels[status] || status;
    document.getElementById('swapViewTitle').textContent = state.customTx ? 'Inspecting deposit' : 'Current swap';
    document.getElementById('swapViewSubtitle').textContent = state.customTx
      ? 'Manual inspection · select Current to follow wallet activity.' : 'Following your wallet’s current deposit automatically.';
    document.getElementById('swapMessage').textContent = {
      LIMIT_WAITING: 'Native limit swap is waiting for its price condition. A completion time cannot be predicted while the order is unfilled.',
      STREAMING: 'Your deposit is being processed in slices. Filled output and any unswapped refund are sent after the stream ends.',
      INBOUND_CONFIRMING: 'Ethereum deposit observed. Waiting for inbound confirmations before swapping.',
      PENDING: 'Waiting for THORChain to observe and confirm this deposit.',
      OUTBOUND_PENDING: 'Swapping has finalized. Delivery and any refund are still being scheduled or observed.',
      COMPLETED: 'Swap finalized and outbound observed by THORChain.',
      COMPLETED_PARTIAL: 'Part of the deposit was swapped. The output and remaining USDC refund are shown below.',
      REFUNDED: 'The deposit was returned in USDC. Refund fees may reduce the returned amount.'
    }[status] || label;
    el.bannerTxLink.textContent = `${formatShortAddress(txId)} ↗`;
    el.bannerTxLink.href = `https://etherscan.io/tx/${txId}`;
    el.bannerStatusBadge.textContent = label;
    el.bannerStatusBadge.className = `badge ${settled ? 'badge-success' : 'badge-subtle'}`;
    el.swapStatusBadge.textContent = label;
    el.swapStatusBadge.className = `stat-badge ${settled ? 'badge-green' : 'badge-cyan'}`;
    el.settlementTypeBadge.textContent = settled ? 'Observed outbounds' : 'Pending settlement';
    el.telemetryStatus.textContent = label;
    [el.settlementCard, el.streamProgressContainer, el.streamMetricsGrid].forEach(node => node.hidden = false);
    document.getElementById('swapTiming').hidden = false;
    document.querySelector('.stages-flow').hidden = false;
    document.querySelector('.telemetry-box').hidden = false;
    state.timing = {...info.timing, finalized, completed: settled, status};
    renderTiming();

    const settlement = info.settlement || {}, inbound = info.inbound || {}, stream = info.streaming;
    const history = info.history_stream;
    el.settlementInbound.textContent = `${formatMoney(inbound.amount)} ${(inbound.asset || 'USDC').split('-')[0]}`;
    el.settlementDelivered.textContent = `${Number(settlement.out_usdp || 0).toFixed(4)} USDP`;
    el.settlementRefund.textContent = `${formatMoney(settlement.refund_usdc || 0)} USDC`;
    el.settlementAffiliate.textContent = `${Number(settlement.affiliate_rune || 0).toFixed(5)} RUNE`;
    el.settlementCollector.textContent = settlement.affiliate_collector ? `Paid to ${formatShortAddress(settlement.affiliate_collector)}` : 'No affiliate outbound observed';
    function outboundLink(node, hash, pending) {
      node.parentElement.style.display = 'block';
      if (hash && !/^0x?0+$/.test(hash)) {
        node.textContent = `${formatShortAddress(hash)} ↗`;
        node.href = `https://etherscan.io/tx/${hash}`;
      } else {
        node.textContent = pending;
        node.removeAttribute('href');
      }
    }
    outboundLink(el.outboundTxLink, settlement.outbound_tx_hash, finalized ? 'No outbound observed yet' : 'Awaiting finalization');
    outboundLink(el.refundTxLink, settlement.refund_tx_hash, settlement.planned_refund_usdc > 0 ? `${formatMoney(settlement.planned_refund_usdc)} USDC scheduled` : 'No refund observed');
    const filled = stream ? stream.swapped_usdc : settlement.swapped_usdc || 0;
    const rate = stream ? stream.effective_rate : settlement.effective_rate || 0;
    document.getElementById('reconSwappedLabel').textContent = settlement.swapped_is_estimate ? 'USDC used (includes fees):' : 'USDC swapped:';
    el.reconSwapped.textContent = `${formatMoney(filled)} USDC`;
    document.getElementById('reconRateLabel').textContent = stream
      ? 'Accumulated rate before delivery costs:' : 'Delivered rate after settlement fees:';
    el.reconRate.textContent = rate ? `${Number(rate).toFixed(6)} USDP/USDC` : '—';
    el.reconTotalBack.textContent = `${formatMoney((settlement.out_usdp || 0) + (settlement.refund_usdc || 0))} at $1 parity`;
    const canReconcile = settled && Number(inbound.amount) > 0
      && ['ETH.USDC', 'USDC'].includes((inbound.asset || '').split('-')[0].toUpperCase())
      && settlement.out_usdp != null && settlement.refund_usdc != null;
    document.getElementById('settlementResult').hidden = !canReconcile;
    if (canReconcile) {
      const difference = Number(settlement.out_usdp) + Number(settlement.refund_usdc) - Number(inbound.amount);
      const result = document.getElementById('settlementTokenDifference');
      result.textContent = `${difference < 0 ? '−' : '+'}${formatMoney(Math.abs(difference))} at $1 parity`;
      result.className = `mono ${difference < 0 ? 'highlight-amber' : difference > 0 ? 'highlight-green' : ''}`;
    }
    const pct = Math.max(0, Math.min(100, Number(info.progress_pct) || 0));
    el.streamProgressBar.style.width = `${pct}%`;
    el.streamPercentText.textContent = `${pct.toFixed(1)}%`;
    el.streamProgressLabel.textContent = finalized ? 'Stream finalized' : 'Slice attempts (includes skips)';
    el.blocksProcessed.textContent = stream?.count ?? history?.count ?? '—';
    el.totalBlocks.textContent = stream?.quantity ?? history?.quantity ?? '—';
    el.swapProgressPct.textContent = `${pct.toFixed(1)}%`;
    const limit = Number((inbound.memo?.split(':')[3] || '').split('/')[0]) / 1e8;
    el.swapTargetFloor.textContent = limit > 0 ? `${formatMoney(limit)} USDP` : 'Unavailable';
    document.getElementById('swapFloorNotice').hidden = !(limit > 0 || status === 'COMPLETED_PARTIAL');
    el.subswapsSuccess.textContent = stream?.success_count ?? history?.success_count ?? '—';
    el.subswapsSkipped.textContent = stream?.skipped_count ?? history?.skipped_count ?? '—';
    el.subswapsSuccessSub.textContent = 'Successful slice attempts';
    el.subswapsSkippedSub.textContent = 'Reported failed slice attempts';
    el.swappedCapital.textContent = `${formatMoney(filled)} USDC`;
    el.remainingCapital.textContent = stream ? `${formatMoney(stream.remaining_usdc)} USDC still in stream` : `${formatMoney(settlement.refund_usdc)} USDC refunded`;
    el.outboundAccumulated.textContent = `${formatMoney(stream ? stream.out_usdp : settlement.out_usdp)} USDP`;
    el.effectiveRateText.textContent = stream ? 'Accumulated · not yet delivered' : 'Observed wallet output';
    el.latestReasonText.textContent = settlement.refund_reason || stream?.latest_reason || document.getElementById('swapMessage').textContent;
    el.inspectedMemoText.textContent = inbound.memo || 'Unavailable';

    resetStages();
    const stages = info.stages || {};
    const stagePairs = [[el.stageInboundObserved, stages.inbound_observed?.completed],
      [el.stageInboundCounted, stages.inbound_confirmation_counted?.completed],
      [el.stageInboundFinalised, stages.inbound_finalised?.completed],
      [el.stageSwapStatus, finalized], [el.stageSwapFinalised, stages.outbound_signed?.completed]];
    stagePairs.forEach(([node, done]) => {
      if (done) { node.className = 'stage-step completed'; node.querySelector('.stage-circle').textContent = '✓'; }
    });
    const observed = stages.inbound_observed;
    if (observed?.completed) el.stageObservedDetail.textContent = `${observed.final_count ?? '—'} observations`;
    if (stream && !finalized) el.stageSwapStatus.className = 'stage-step active';
    el.stageStreamingDetail.textContent = finalized ? 'Finalized' : stream ? `${stream.count}/${stream.quantity} attempts` : 'Awaiting execution';
    el.stageOutboundDetail.textContent = settled ? 'Outbound observed' : finalized ? 'Pending delivery' : 'After finalization';
    if (finalized && !settled) el.stageSwapFinalised.className = 'stage-step active';
  }

  function daemonPayload() {
    return {
      mode: document.getElementById('sniperExecutionMode').value,
      amount: el.sniperAmount.value || '10000',
      min_profit_bps: el.sniperProfitBps.value || '2',
      min_net_profit: document.getElementById('sniperMinNetProfit').value || '1',
      poll_interval: el.sniperPollSec.value || '3',
      affiliate: el.sniperAffiliateName.value.trim(),
      affiliate_bps: el.sniperAffiliateBps.value.trim() || '0',
      floor_mode: el.sniperFloorMode.value,
      confirm: 'START_SNIPER'
    };
  }

  function updateDaemonModeControls() {
    const resting = document.getElementById('sniperExecutionMode').value === 'resting';
    document.getElementById('sniperMinNetProfitGroup').hidden = resting;
    document.getElementById('sniperMinNetProfit').disabled = resting;
    document.getElementById('sniperModeDescription').textContent = resting
      ? 'Waits for the slice-price condition, deposits the full amount using the maximum permitted slices, one every 2 blocks, then exits after confirmation. Completion time depends on the resolved slice count. Funds remain locked until settlement. This mode skips the immediate net-profit check; fees can leave a loss even with a positive slice floor.'
      : 'Waits for immediate fills that meet your minimum modeled net profit after fees and gas. A favorable full-fill quote alone does not trigger a deposit.';
  }

  function updateSniperStatus(sniper) {
    if (!sniper) return;
    const isRunning = Boolean(sniper.running);
    if (isRunning) {
      el.sniperDot.className = 'status-indicator-dot online';
      el.sniperStatusText.textContent = 'DAEMON RUNNING';
      el.sniperPidBadge.textContent = `PID: ${sniper.pid || 'Active'}`;
      el.startSniperBtn.disabled = true;
      el.stopSniperBtn.disabled = false;
    } else if (sniper.pending_swap) {
      // A manual or resting swap owns the wallet until it places; Stop cancels a waiting one.
      const p = sniper.pending_swap;
      const waiting = p.state === 'waiting';
      el.sniperDot.className = 'status-indicator-dot online';
      el.sniperStatusText.textContent = waiting
        ? `RESTING ORDER WAITING: ${p.detail || 'pool rate below the slice fill rate'}`
        : 'SWAP BEING PLACED';
      el.sniperPidBadge.textContent = `PID: ${p.pid}`;
      el.startSniperBtn.disabled = true;
      el.stopSniperBtn.disabled = !waiting;
    } else {
      el.sniperDot.className = 'status-indicator-dot';
      el.sniperStatusText.textContent = 'DAEMON OFFLINE';
      el.sniperPidBadge.textContent = 'PID: None';
      el.startSniperBtn.disabled = false;
      el.stopSniperBtn.disabled = true;
    }
    state.pendingSwap = sniper.pending_swap || null;
  }

  function renderQuote(q) {
    el.quoteResultCard.hidden = false;
    el.quoteResultCard.style.display = 'block';
    el.quoteExpectedUsdp.textContent = formatMoney(q.expected_output);

    const netProfit = q.net_profit;
    const profitBps = q.profit_bps;

    if (q.is_profitable) {
      el.quoteProfitBadge.className = 'profit-badge badge-positive';
      el.quoteProfitBadge.textContent = `+${profitBps.toFixed(1)} bps`;
      el.quoteNetProfit.className = 'val mono highlight-green';
      el.quoteNetProfit.textContent = `+${formatMoney(netProfit)} at parity`;
    } else {
      el.quoteProfitBadge.className = 'profit-badge badge-negative';
      el.quoteProfitBadge.textContent = `${profitBps.toFixed(1)} bps`;
      el.quoteNetProfit.className = 'val mono highlight-amber';
      el.quoteNetProfit.textContent = `-${formatMoney(Math.abs(netProfit))} at parity`;
    }

    const mins = Math.ceil(q.total_seconds / 60);
    el.quoteStreamBlocks.textContent = `${q.streaming_blocks} blocks (~${mins} mins)`;

    const fees = q.fees || {};
    el.feeOutbound.textContent = `${formatMoney(fees.outbound || 0)} USDP`;
    el.feeLiquidity.textContent = `${formatMoney(fees.liquidity || 0)} USDP`;
    el.feeAffiliate.textContent = `${formatMoney(fees.affiliate || 0)} USDP`;

    el.memoPreviewText.textContent = q.memo || '--';

    const r = q.resting;
    if (el.quoteRestingInfo) el.quoteRestingInfo.style.display = r ? '' : 'none';
    if (r) {
      el.restingSlices.textContent = `${r.slices} × ${formatMoney(r.slice_usdc)} USDC, every ${r.interval_blocks} blocks (~${r.hours} h)`;
      el.restingFillRate.textContent = r.slice_fill_rate
        ? `${Number(r.slice_fill_rate).toFixed(5)} USDP/USDC (now ${Number(r.spot_rate).toFixed(5)})`
        : 'Unavailable';
    }
  }

  function renderLogs(logs) {
    const isScrolledToBottom = el.terminalLogs.scrollHeight - el.terminalLogs.clientHeight <= el.terminalLogs.scrollTop + 30;

    let html = '';
    for (const line of logs) {
      let cls = 'log-line';
      if (line.includes('CONFIRMED') || line.includes('SUCCESS') || line.includes('🎯') || line.includes('COMPLETED')) cls += ' success';
      else if (line.includes('ERROR') || line.includes('reverted') || line.includes('failed')) cls += ' error';
      else if (line.includes('WARN') || line.includes('Waiting') || line.includes('less than') || line.includes('skips')) cls += ' warning';
      else if (line.includes('INFO') || line.includes('=====')) cls += ' system';
      html += `<div class="${cls}">${escapeHtml(line)}</div>`;
    }
    el.terminalLogs.innerHTML = html;

    if (el.autoscrollCheck.checked && isScrolledToBottom) {
      el.terminalLogs.scrollTop = el.terminalLogs.scrollHeight;
    }
  }

  function escapeHtml(text) {
    const div = document.createElement('div');
    div.textContent = text;
    return div.innerHTML;
  }

  // --- Actions & Event Listeners ---
  function setupEvents() {
    // Tab switching
    el.tabBtns.forEach(btn => {
      btn.addEventListener('click', () => {
        el.tabBtns.forEach(b => b.classList.remove('active'));
        el.tabPanes.forEach(p => p.classList.remove('active'));
        btn.classList.add('active');
        el.tabBtns.forEach(b => b.setAttribute('aria-selected', String(b === btn)));
        const target = btn.dataset.tab;
        document.getElementById(target).classList.add('active');
        if (target === 'usdtQuoterTab' && !state.otherQuote) requestUsdtQuote();
      });
    });

    // Quick chip buttons for amount
    el.chipBtns.forEach(chip => {
      chip.addEventListener('click', () => {
        el.chipBtns.forEach(c => c.classList.remove('active'));
        chip.classList.add('active');
        el.quoteAmount.value = chip.dataset.val;
        requestQuote();
      });
    });

    [el.quoteAmount, el.quoteSlippage, el.quoteAffiliateName, el.quoteAffiliateBps, el.quoteFloorMode, el.quoteExecMode].forEach(node => {
      node.addEventListener('input', invalidateQuote);
    });
    document.getElementById('otherQuotePair').addEventListener('change', () => {
      const source = document.getElementById('otherQuotePair').value;
      const target = source === 'usdp' ? 'USDC' : 'USDP';
      invalidateOtherQuote();
      document.getElementById('otherQuoteTabBtn').textContent = `${source.toUpperCase()} → ${target}`;
      document.getElementById('otherQuoteInputLabel').textContent = `Input amount (${source.toUpperCase()})`;
      document.getElementById('otherQuoteOutputLabel').textContent = `Market estimate (${target})`;
      requestUsdtQuote();
    });
    [el.usdtQuoteAmount, el.usdtQuoteAffiliateName, el.usdtQuoteAffiliateBps, document.getElementById('otherQuoteSlippage')].forEach(node => node.addEventListener('input', () => invalidateOtherQuote()));
    // Quote-only pairs
    el.getUsdtQuoteBtn.addEventListener('click', requestUsdtQuote);
    el.usdtQuoteFloorMode.addEventListener('change', () => {
      document.getElementById('otherQuoteSlippageGroup').hidden = el.usdtQuoteFloorMode.value !== 'market';
      document.getElementById('otherQuoteFloorNotice').hidden = el.usdtQuoteFloorMode.value === 'market';
      invalidateOtherQuote();
      requestUsdtQuote();
    });
    el.usdtQuoteAmount.addEventListener('keydown', (e) => {
      if (e.key === 'Enter') {
        e.preventDefault();
        requestUsdtQuote();
      }
    });
    el.usdtChipBtns.forEach(chip => {
      chip.addEventListener('click', () => {
        el.usdtChipBtns.forEach(c => c.classList.remove('active'));
        chip.classList.add('active');
        el.usdtQuoteAmount.value = chip.dataset.val;
        requestUsdtQuote();
      });
    });

    // Quoting trigger & Floor mode change
    el.getQuoteBtn.addEventListener('click', requestQuote);
    if (el.quoteFloorMode) {
      el.quoteFloorMode.addEventListener('change', requestQuote);
    }
    if (el.quoteExecMode) {
      el.quoteExecMode.addEventListener('change', requestQuote);
    }

    // --- Custom Deposit Tx Inspector Handlers ---
    function chipLabel(prefix, dep) {
      if (!dep) return `${prefix} (none yet)`;
      return `${prefix} (${formatShortAddress(dep.tx_hash)} · ${dep.status || 'unknown'})`;
    }

    // Latest/Previous chips follow the wallet's actual deposits, CLI-placed ones included.
    async function refreshLatestDeposits() {
      try {
        const res = await fetch('/api/latest_deposits');
        const data = await res.json();
        if (!data.ok) return;
        const [latest, previous] = data.deposits || [];
        state.latestTx = latest ? latest.tx_hash : null;
        state.previousTx = previous ? previous.tx_hash : null;
        el.chipLatestSwap.disabled = !latest;
        el.chipPreviousSwap.disabled = !previous;
        if (el.chipLatestSwap) el.chipLatestSwap.querySelector('span').textContent = chipLabel('Latest deposit', latest);
        if (el.chipPreviousSwap) el.chipPreviousSwap.querySelector('span').textContent = chipLabel('Previous deposit', previous);
      } catch (err) {
        console.warn('Latest deposits lookup error:', err);
      }
    }

    async function loadCustomTx(hash) {
      const clean = (hash || '').trim();
      if (clean && !/^(0x)?[a-f0-9]{64}$/i.test(clean)) {
        document.getElementById('swapMessage').textContent = 'Enter a valid Ethereum transaction hash (64 hex characters).';
        return;
      }
      state.customTx = clean || null;
      if (el.customTxInput) {
        el.customTxInput.value = clean;
      }
      if (clean) {
        if (el.chipLatestSwap) el.chipLatestSwap.classList.toggle('active', clean.toLowerCase() === (state.latestTx || '').toLowerCase());
        if (el.chipPreviousSwap) el.chipPreviousSwap.classList.toggle('active', clean.toLowerCase() === (state.previousTx || '').toLowerCase());
        if (el.chipLiveQueue) el.chipLiveQueue.classList.remove('active');
      } else {
        if (el.chipLatestSwap) el.chipLatestSwap.classList.remove('active');
        if (el.chipPreviousSwap) el.chipPreviousSwap.classList.remove('active');
        if (el.chipLiveQueue) el.chipLiveQueue.classList.add('active');
      }
      renderEmptySwap({status: 'LOADING', message: 'Loading selected swap…'});
      await fetchStatus();
    }

    el.loadTxBtn.addEventListener('click', () => {
      loadCustomTx(el.customTxInput.value);
    });

    el.customTxInput.addEventListener('keydown', (e) => {
      if (e.key === 'Enter') {
        e.preventDefault();
        loadCustomTx(el.customTxInput.value);
      }
    });

    if (el.clearTxBtn) {
      el.clearTxBtn.addEventListener('click', () => {
        loadCustomTx('');
      });
    }

    if (el.chipLatestSwap) {
      el.chipLatestSwap.addEventListener('click', async () => {
        await refreshLatestDeposits();
        if (state.latestTx) loadCustomTx(state.latestTx);
      });
    }

    if (el.chipPreviousSwap) {
      el.chipPreviousSwap.addEventListener('click', async () => {
        await refreshLatestDeposits();
        if (state.previousTx) loadCustomTx(state.previousTx);
      });
    }

    refreshLatestDeposits();
    document.addEventListener('refreshDeposits', refreshLatestDeposits);

    if (el.chipLiveQueue) {
      el.chipLiveQueue.addEventListener('click', () => {
        loadCustomTx('');
      });
    }

    // Copy Operator Address
    el.copyAddressBtn.addEventListener('click', (e) => {
      e.stopPropagation();
      const addr = el.operatorPill.dataset.address;
      if (addr) {
        navigator.clipboard.writeText(addr);
        const originalText = el.operatorAddress.textContent;
        el.operatorAddress.textContent = 'COPIED!';
        setTimeout(() => {
          el.operatorAddress.textContent = originalText;
        }, 1500);
      }
    });

    el.operatorPill.addEventListener('click', () => {
      const addr = el.operatorPill.dataset.address;
      if (addr) window.open(`https://etherscan.io/address/${addr}`, '_blank');
    });

    // Refresh control
    el.refreshBtn.addEventListener('click', () => {
      fetchStatus();
      fetchLogs();
    });

    // Clear Terminal Logs
    el.clearLogsBtn.addEventListener('click', () => {
      el.terminalLogs.innerHTML = '<div class="log-line system">[SYSTEM] Console cleared by operator.</div>';
    });

    // Verify Affiliate Button
    if (el.verifyAffiliateBtn) {
      el.verifyAffiliateBtn.addEventListener('click', async () => {
        const name = el.quoteAffiliateName ? el.quoteAffiliateName.value.trim() : 'salactite';
        if (!name) return;
        el.verifyAffiliateBtn.disabled = true;
        el.affiliateLookupStatus.textContent = 'Checking THORChain...';
        try {
          const res = await fetch(`/api/affiliate?name=${encodeURIComponent(name)}`);
          const data = await res.json();
          if (data.ok && !data.thor_alias) {
            // THORChain pays affiliate fees to the THOR alias; swaps naming this handle get refunded.
            el.affiliateLookupStatus.className = 'form-label-right highlight-amber';
            el.affiliateLookupStatus.textContent = '⚠️ No THOR alias: swaps will refund';
            alert(`⚠️ THORName '${data.name}' has no THOR alias.\n\nTHORChain refunds swaps that name it with a non-zero affiliate fee ("swap affiliate address is empty"). Add a THOR alias, or set affiliate bps to 0.\n\nAliases: ${JSON.stringify(data.aliases)}`);
          } else if (data.ok) {
            el.affiliateLookupStatus.className = 'form-label-right highlight-green';
            el.affiliateLookupStatus.textContent = `✓ Valid (${data.accumulated_rune.toFixed(4)} RUNE accrued)`;
            alert(`✅ THORName '${data.name}' Registered on THORChain!\n\nOwner: ${data.owner}\nTHOR alias: ${data.thor_alias}\nPreferred Asset: ${data.preferred_asset || 'Native RUNE'}\nAccumulated Collector Fees: ${data.accumulated_rune} RUNE\nAuto-Payout Threshold: ${data.threshold_rune} RUNE\nAliases: ${JSON.stringify(data.aliases)}`);
          } else {
            el.affiliateLookupStatus.className = 'form-label-right highlight-amber';
            el.affiliateLookupStatus.textContent = '⚠️ Unregistered handle';
            alert(`⚠️ THORName '${name}' is not currently registered on THORChain.`);
          }
        } catch (err) {
          el.affiliateLookupStatus.textContent = 'Error checking handle';
        } finally {
          el.verifyAffiliateBtn.disabled = false;
        }
      });
    }

    // Sniper Daemon Controls
    document.getElementById('sniperExecutionMode').addEventListener('change', updateDaemonModeControls);
    updateDaemonModeControls();
    el.startSniperBtn.addEventListener('click', async () => {
      const payload = daemonPayload();
      const resting = payload.mode === 'resting';
      const modeDescription = resting
        ? 'RESTING: wait for slice price, place one stream, then exit. The full deposit is locked until settlement. The immediate net-profit check is skipped.'
        : `IMMEDIATE: wait for modeled net profit of at least ${payload.min_net_profit} USD after fees and gas.`;
      if (!confirm(`Start daemon in LIVE execution mode?\n\n${modeDescription}\nAmount: ${payload.amount} USDC\nQuote trigger buffer: ${payload.min_profit_bps} bps\nSlice price floor: ${payload.floor_mode}\nAffiliate: ${payload.affiliate} (${payload.affiliate_bps} bps)\n\n${SLICE_FLOOR_FEE_NOTICE}`)) return;

      el.startSniperBtn.disabled = true;
      try {
        const res = await fetch('/api/sniper/start', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(payload)
        });
        const data = await res.json();
        if (data.ok) {
          alert('✅ ' + data.message);
          fetchStatus();
          fetchLogs();
        } else {
          alert('❌ Failed to start sniper: ' + (data.error || 'Unknown error'));
        }
      } catch (err) {
        alert('Network error: ' + err.message);
      } finally {
        el.startSniperBtn.disabled = false;
      }
    });

    el.stopSniperBtn.addEventListener('click', async () => {
      const question = state.pendingSwap && state.pendingSwap.state === 'waiting'
        ? 'Cancel the waiting resting order? Nothing has been sent yet.'
        : 'Are you sure you want to stop the running sniper daemon?';
      if (!confirm(question)) return;
      el.stopSniperBtn.disabled = true;
      try {
        const res = await fetch('/api/sniper/stop', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({})
        });
        const data = await res.json();
        if (data.ok) {
          alert('🛑 ' + data.message);
          fetchStatus();
          fetchLogs();
        } else {
          alert('❌ Failed to stop: ' + (data.error || 'Unknown error'));
        }
      } catch (err) {
        alert('Network error: ' + err.message);
      } finally {
        el.stopSniperBtn.disabled = false;
      }
    });

    // --- Modal Live Swap Handlers ---
    el.openSwapModalBtn.addEventListener('click', () => {
      const current = state.lastQuote;
      if (!current || current.fingerprint !== quoteFingerprint() || (current.expiry && Number(current.expiry) * 1000 <= Date.now())) {
        invalidateQuote();
        return;
      }
      const amount = el.quoteAmount.value || '10000';
      const slippage = el.quoteSlippage.value || '1';

      // The floor is the memo's limit field (1e8 units), not the expected output.
      // Only trust it when the last quote was for the amount about to be sent.
      let floorText = 'Get a quote for this amount first';
      const q = state.lastQuote;
      if (q && Number(q.input_amount) === Number(amount) && q.memo) {
        const limit = Number((q.memo.split(':')[3] || '').split('/')[0]);
        if (limit > 0) floorText = `${formatMoney(limit / 1e8)} USDP at full input size (market estimate ${formatMoney(q.expected_output)})`;
      }

      el.modalInputAmount.textContent = `${formatMoney(amount)} USDC`;
      el.modalMinOutput.textContent = floorText;
      el.modalSlippage.textContent = `${slippage} bps`;
      const resting = el.quoteExecMode && el.quoteExecMode.value === 'resting';
      if (el.modalOrderType) {
        el.modalOrderType.textContent = resting
          ? 'Resting order: maximum permitted slices, one every 2 blocks, checked against the memo target'
          : 'Instant: sniper sends only a size that clears its profit check';
      }
      el.modalConfirmText.value = '';
      el.submitLiveSwapBtn.disabled = true;

      el.swapModal.showModal();
    });

    el.closeModalBtn.addEventListener('click', () => el.swapModal.close());
    el.cancelModalBtn.addEventListener('click', () => el.swapModal.close());

    el.modalConfirmText.addEventListener('input', () => {
      el.submitLiveSwapBtn.disabled = el.modalConfirmText.value.trim() !== 'EXECUTE_LIVE_SWAP';
    });

    el.submitLiveSwapBtn.addEventListener('click', async () => {
      if (!state.lastQuote || state.lastQuote.fingerprint !== quoteFingerprint() || (state.lastQuote.expiry && Number(state.lastQuote.expiry) * 1000 <= Date.now())) {
        el.swapModal.close();
        invalidateQuote();
        return;
      }
      const amount = el.quoteAmount.value || '10000';
      const slippage = el.quoteSlippage.value || '1';
      const affiliate = el.quoteAffiliateName ? el.quoteAffiliateName.value.trim() : 'symbiosis';
      const affiliateBps = el.quoteAffiliateBps ? el.quoteAffiliateBps.value.trim() : '1';
      const floorMode = el.quoteFloorMode ? el.quoteFloorMode.value : '2bps';

      el.submitLiveSwapBtn.disabled = true;
      el.submitLiveSwapBtn.textContent = 'Broadcasting to Ethereum...';

      try {
        const res = await fetch('/api/swap/live', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            amount: amount,
            slippage_bps: slippage,
            affiliate: affiliate,
            affiliate_bps: affiliateBps,
            floor_mode: floorMode,
            mode: el.quoteExecMode ? el.quoteExecMode.value : 'instant',
            confirm: 'EXECUTE_LIVE_SWAP'
          })
        });
        const data = await res.json();
        if (data.ok && data.waiting) {
          alert(`⏳ RESTING ORDER WAITING (PID ${data.pid})\n\n${data.message}`);
          el.swapModal.close();
          fetchStatus();
          fetchLogs();
        } else if (data.ok) {
          const title = data.pending ? '⏳ LIVE SWAP BROADCAST (UNCONFIRMED)' : '🚀 LIVE SWAP DEPOSIT CONFIRMED';
          alert(`${title}\n\nTx Hash: ${data.tx_hash}\n\n${data.message}`);
          el.swapModal.close();
          loadCustomTx('');
          fetchLogs();
        } else {
          alert('❌ Swap Failed: ' + (data.error || 'Transaction reverted or failed to broadcast'));
          if (data.tx_hash) loadCustomTx(data.tx_hash);
        }
      } catch (err) {
        alert('Network error: ' + err.message);
      } finally {
        el.submitLiveSwapBtn.textContent = 'Confirm & Broadcast Live Swap';
        el.submitLiveSwapBtn.disabled = el.modalConfirmText.value.trim() !== 'EXECUTE_LIVE_SWAP';
      }
    });
  }

  // Polling loop. /api/status can take several seconds (THORNode + Ethereum RPC),
  // so wait for each round to finish before scheduling the next instead of
  // stacking overlapping requests with setInterval.
  async function pollOnce() {
    try {
      await Promise.all([fetchStatus(), fetchLogs()]);
      document.dispatchEvent(new Event('refreshDeposits'));
    } finally {
      state.pollTimer = setTimeout(pollOnce, state.pollingIntervalMs);
    }
  }

  function startPolling() {
    requestQuote();
    pollOnce();
  }

  // Init
  document.addEventListener('DOMContentLoaded', () => {
    renderEmptySwap({status: 'LOADING', message: 'Looking for current wallet swaps…'});
    setupEvents();
    startPolling();
    setInterval(renderTiming, 1000);
  });
})();
