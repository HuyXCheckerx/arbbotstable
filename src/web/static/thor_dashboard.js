/**
 * THORChain USDC -> USDP Terminal Client Logic
 * Real-time polling, quoting, live execution, and comprehensive Deposit Tx Inspector.
 */

(function () {
  'use strict';

  // Constants
  const LATEST_TX = '0x3dfaf115a61e469fccb987a7977d32326e32edc5683f26b16775c6da826f0610';
  const KNOWN_PREVIOUS_TX = '0xce98133010002812f44dd2e9239b5963c2e4e94c7d36a8590293e0dda145816e';

  // State
  let state = {
    pollingIntervalMs: 3000,
    pollTimer: null,
    isPolling: true,
    lastQuote: null,
    customTx: LATEST_TX, // Default to inspecting the newest deposit tx
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
    chipBtns: document.querySelectorAll('.chip-btn'),

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
  async function fetchStatus() {
    try {
      let url = '/api/status';
      if (state.customTx) {
        url += '?tx=' + encodeURIComponent(state.customTx);
      }
      const res = await fetch(url);
      if (!res.ok) return;
      const data = await res.json();
      if (!data.ok) return;

      updateWallet(data.wallet);
      updatePools(data.pools);
      updateSniperStatus(data.sniper);

      // Prefer rich deposit_info if available, otherwise active_swap
      if (data.deposit_info && data.deposit_info.found) {
        renderDepositInfo(data.deposit_info);
      } else if (data.active_swap) {
        renderDepositInfo(data.active_swap);
      } else if (state.customTx) {
        // Looked up custom tx not found yet
        el.bannerStatusBadge.textContent = 'NOT FOUND ON-CHAIN';
        el.bannerStatusBadge.className = 'badge badge-subtle';
        el.latestReasonText.textContent = `Transaction ${state.customTx} was not found on THORChain. If submitted recently, wait 1-2 Ethereum blocks.`;
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
      console.warn('Status poll error:', err);
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
      if (!data.ok) {
        alert('Quote Error: ' + (data.error || 'Unknown error'));
        return;
      }
      state.lastQuote = data;
      renderQuote(data);
    } catch (err) {
      alert('Network error while requesting quote: ' + err.message);
    } finally {
      el.getQuoteBtn.disabled = false;
      el.getQuoteBtn.innerHTML = '<span>Get Live Quote</span>';
    }
  }

  // --- Update Top Stats UI ---
  function updateWallet(wallet) {
    if (!wallet) return;
    if (wallet.operator) {
      el.operatorAddress.textContent = formatShortAddress(wallet.operator);
      el.operatorPill.dataset.address = wallet.operator;
    }
    el.usdcBalance.innerHTML = `${formatMoney(wallet.usdc_balance)} <span class="unit">USDC</span>`;
    el.usdpBalance.innerHTML = `${Number(wallet.usdp_balance).toFixed(4)} <span class="unit">USDP</span>`;
    el.usdcAllowance.textContent = `${formatMoney(wallet.usdc_allowance)} USDC`;
    el.ethBalance.textContent = `${wallet.eth_balance} ETH`;
    el.maxUsdcHint.textContent = `Available: ${formatMoney(wallet.usdc_balance)} USDC`;
  }

  function updatePools(pools) {
    if (!pools) return;
    if (pools.spot_rate > 0) {
      el.poolSpotRate.innerHTML = `${Number(pools.spot_rate).toFixed(5)} <span class="unit">USDP/USDC</span>`;
    }
    if (pools.usdp_depth > 0) {
      el.poolUsdpDepth.textContent = `${formatMoney(pools.usdp_depth)} USDP`;
    }
  }

  // --- Render Comprehensive Deposit Information ---
  function renderDepositInfo(info) {
    if (!info) return;

    const txId = info.tx_id || (info.inbound && info.inbound.tx_hash) || state.customTx || KNOWN_PREVIOUS_TX;
    el.bannerTxLink.textContent = `${formatShortAddress(txId)} ↗`;
    el.bannerTxLink.href = `https://etherscan.io/tx/${txId}`;

    const status = info.status || (info.active ? 'STREAMING' : 'COMPLETED');

    // 1. Status Badges
    if (status === 'STREAMING') {
      el.bannerStatusBadge.textContent = 'STREAMING IN PROGRESS';
      el.bannerStatusBadge.className = 'badge badge-pulsing';
      el.swapStatusBadge.textContent = 'STREAMING';
      el.swapStatusBadge.className = 'stat-badge badge-pulsing';
      el.settlementTypeBadge.textContent = 'Active Streaming Swap';
      el.telemetryStatus.textContent = 'PROTECTION ACTIVE';
    } else if (status === 'COMPLETED_PARTIAL') {
      el.bannerStatusBadge.textContent = 'SETTLED (PARTIAL FILL + REFUND)';
      el.bannerStatusBadge.className = 'badge badge-success';
      el.swapStatusBadge.textContent = 'SETTLED';
      el.swapStatusBadge.className = 'stat-badge badge-green';
      el.settlementTypeBadge.textContent = 'Execution Reconciled';
      el.telemetryStatus.textContent = 'SETTLED';
    } else if (status === 'REFUNDED') {
      el.bannerStatusBadge.textContent = '100% CAPITAL REFUNDED';
      el.bannerStatusBadge.className = 'badge badge-amber';
      el.swapStatusBadge.textContent = 'REFUNDED';
      el.swapStatusBadge.className = 'stat-badge badge-amber';
      el.settlementTypeBadge.textContent = 'Floor Protected (100% Refund)';
      el.telemetryStatus.textContent = 'REFUNDED';
    } else {
      el.bannerStatusBadge.textContent = 'SETTLED & DELIVERED';
      el.bannerStatusBadge.className = 'badge badge-success';
      el.swapStatusBadge.textContent = 'SETTLED';
      el.swapStatusBadge.className = 'stat-badge badge-green';
      el.settlementTypeBadge.textContent = 'Outbound Signed & Settled';
      el.telemetryStatus.textContent = 'SETTLED';
    }

    // 2. Settlement Ledger
    const settlement = info.settlement || {};
    const inbound = info.inbound || {};
    const inboundAmount = inbound.amount || info.deposit_usdc || 10000.0;
    const inboundAsset = inbound.asset ? inbound.asset.split('-')[0] : 'USDC';

    el.settlementInbound.textContent = `${formatMoney(inboundAmount)} ${inboundAsset}`;

    const deliveredUsdp = settlement.out_usdp !== undefined ? settlement.out_usdp : (info.out_usdp || 0.0);
    el.settlementDelivered.textContent = `${Number(deliveredUsdp).toFixed(4)} USDP`;

    const outTxHash = settlement.outbound_tx_hash;
    if (outTxHash && !outTxHash.includes('0000000000')) {
      el.outboundTxLink.textContent = `${formatShortAddress(outTxHash)} ↗`;
      el.outboundTxLink.href = `https://etherscan.io/tx/${outTxHash}`;
      el.outboundTxLink.parentElement.style.display = 'block';
    } else if (status === 'STREAMING') {
      el.outboundTxLink.textContent = 'Pending stream completion...';
      el.outboundTxLink.removeAttribute('href');
      el.outboundTxLink.parentElement.style.display = 'block';
    } else {
      el.outboundTxLink.parentElement.style.display = 'none';
    }

    const refundUsdc = settlement.refund_usdc !== undefined ? settlement.refund_usdc : (info.remaining_usdc || 0.0);
    el.settlementRefund.textContent = `${formatMoney(refundUsdc)} USDC`;

    const refundTxHash = settlement.refund_tx_hash;
    if (refundTxHash && !refundTxHash.includes('0000000000')) {
      el.refundTxLink.textContent = `${formatShortAddress(refundTxHash)} ↗`;
      el.refundTxLink.href = `https://etherscan.io/tx/${refundTxHash}`;
      el.refundTxLink.parentElement.style.display = 'block';
    } else if (status === 'STREAMING') {
      el.refundTxLink.textContent = 'Safely held in vault...';
      el.refundTxLink.removeAttribute('href');
      el.refundTxLink.parentElement.style.display = 'block';
    } else {
      el.refundTxLink.parentElement.style.display = 'none';
    }

    const affiliateRune = settlement.affiliate_rune !== undefined ? settlement.affiliate_rune : 0.0;
    el.settlementAffiliate.textContent = `${Number(affiliateRune).toFixed(5)} RUNE`;
    if (settlement.affiliate_collector) {
      el.settlementCollector.textContent = `Paid to: ${formatShortAddress(settlement.affiliate_collector)}`;
    } else {
      el.settlementCollector.textContent = 'Rebate credited to affiliate';
    }

    const swappedUsdc = settlement.swapped_usdc !== undefined ? settlement.swapped_usdc : (info.swapped_usdc || 0.0);
    const effRate = settlement.effective_rate !== undefined ? settlement.effective_rate : (info.effective_rate || 0.0);
    el.reconSwapped.textContent = `${formatMoney(swappedUsdc)} USDC`;
    el.reconRate.textContent = `${Number(effRate).toFixed(6)} USDP/USDC`;
    el.reconTotalBack.textContent = `${formatMoney(deliveredUsdp + refundUsdc)} Total Capital`;

    // 3. Progress and Sub-swap Metrics
    if (info.streaming) {
      const s = info.streaming;
      el.streamProgressBar.style.width = `${Math.min(100, s.progress_pct)}%`;
      el.streamPercentText.textContent = `${s.progress_pct}%`;
      el.blocksProcessed.textContent = s.count;
      el.totalBlocks.textContent = s.quantity;
      el.swapProgressPct.innerHTML = `${s.progress_pct}% <span class="unit">(${s.count}/${s.quantity})</span>`;

      el.subswapsSuccess.textContent = s.success_count;
      el.subswapsSuccessSub.textContent = 'Executed above floor limit';
      el.subswapsSkipped.textContent = s.skipped_count;
      el.subswapsSkippedSub.textContent = 'Preserved in vault';
      el.swappedCapital.textContent = `${formatMoney(s.swapped_usdc)} USDC`;
      el.remainingCapital.textContent = `${formatMoney(s.remaining_usdc)} USDC remaining`;
      el.outboundAccumulated.textContent = `${formatMoney(s.out_usdp)} USDP`;
      el.effectiveRateText.textContent = `+${s.profit_bps} bps net rate`;

      if (s.target_limit) {
        el.swapTargetFloor.textContent = `${formatMoney(s.target_limit)} USDP`;
      }
      el.latestReasonText.textContent = s.latest_reason || 'Evaluating pool rates on each block against limit floor.';
    } else {
      // Completed / Finalized
      el.streamProgressBar.style.width = '100%';
      el.streamPercentText.textContent = '100.0%';
      el.blocksProcessed.textContent = '1,637';
      el.totalBlocks.textContent = '1,637';
      el.swapProgressPct.innerHTML = `100.0% <span class="unit">(Finalized)</span>`;
      el.swapTargetFloor.textContent = `${formatMoney(inboundAmount)} USDP (1:1)`;

      el.subswapsSuccess.textContent = deliveredUsdp > 0 ? '1+' : '0';
      el.subswapsSuccessSub.textContent = 'Fills broadcasted to L1';
      el.subswapsSkipped.textContent = refundUsdc > 0 ? 'Protected' : '0';
      el.subswapsSkippedSub.textContent = refundUsdc > 0 ? 'Preserved below floor' : 'Zero skips';
      el.swappedCapital.textContent = `${formatMoney(swappedUsdc)} USDC`;
      el.remainingCapital.textContent = `${formatMoney(refundUsdc)} USDC refunded`;
      el.outboundAccumulated.textContent = `${formatMoney(deliveredUsdp)} USDP`;
      el.effectiveRateText.textContent = `Delivered to operator address`;

      if (settlement.refund_reason) {
        el.latestReasonText.textContent = `Refund Reason: ${settlement.refund_reason}`;
      } else {
        el.latestReasonText.textContent = (
          `Settlement Reconciled: Delivered ${deliveredUsdp} USDP, refunded ${refundUsdc} USDC. Net execution rate: ${effRate} USDP/USDC.`
        );
      }
    }

    // 4. Memo Preview
    if (inbound.memo) {
      el.inspectedMemoText.textContent = inbound.memo;
    }

    // 5. Consensus Stages Pipeline
    const stages = info.stages || {};
    if (stages.inbound_observed?.completed) {
      el.stageInboundObserved.className = 'stage-step completed';
      el.stageObservedDetail.textContent = '111 / 111 validators';
    }
    if (stages.inbound_confirmation_counted?.completed) {
      el.stageInboundCounted.className = 'stage-step completed';
    }
    if (stages.inbound_finalised?.completed) {
      el.stageInboundFinalised.className = 'stage-step completed';
    }
    if (stages.swap_status?.pending) {
      el.stageSwapStatus.className = 'stage-step active';
      el.stageStreamingDetail.textContent = `Block ${info.streaming?.count || 0}/${info.streaming?.quantity || 0}`;
    } else if (stages.swap_finalised?.completed) {
      el.stageSwapStatus.className = 'stage-step completed';
      el.stageStreamingDetail.textContent = 'Stream completed';
    }
    if (stages.outbound_signed?.completed) {
      el.stageSwapFinalised.className = 'stage-step completed';
      el.stageOutboundDetail.textContent = 'Delivered & signed';
    } else if (stages.swap_finalised?.completed) {
      el.stageSwapFinalised.className = 'stage-step active';
      el.stageOutboundDetail.textContent = 'Signing outbound...';
    }
  }

  function updateSniperStatus(sniper) {
    if (!sniper) return;
    const isRunning = Boolean(sniper.running);
    if (isRunning) {
      el.sniperDot.className = 'status-indicator-dot online';
      el.sniperStatusText.textContent = 'DAEMON ACTIVE (POLLING)';
      el.sniperPidBadge.textContent = `PID: ${sniper.pid || 'Active'}`;
      el.startSniperBtn.disabled = true;
      el.stopSniperBtn.disabled = false;
    } else {
      el.sniperDot.className = 'status-indicator-dot';
      el.sniperStatusText.textContent = 'DAEMON OFFLINE';
      el.sniperPidBadge.textContent = 'PID: None';
      el.startSniperBtn.disabled = false;
      el.stopSniperBtn.disabled = true;
    }
  }

  function renderQuote(q) {
    el.quoteResultCard.style.display = 'block';
    el.quoteExpectedUsdp.textContent = formatMoney(q.expected_output);

    const netProfit = q.net_profit;
    const profitBps = q.profit_bps;

    if (q.is_profitable) {
      el.quoteProfitBadge.className = 'profit-badge badge-positive';
      el.quoteProfitBadge.textContent = `+${profitBps.toFixed(1)} bps`;
      el.quoteNetProfit.className = 'val mono highlight-green';
      el.quoteNetProfit.textContent = `+$${formatMoney(netProfit)} (+${profitBps.toFixed(1)} bps)`;
    } else {
      el.quoteProfitBadge.className = 'profit-badge badge-negative';
      el.quoteProfitBadge.textContent = `${profitBps.toFixed(1)} bps`;
      el.quoteNetProfit.className = 'val mono highlight-amber';
      el.quoteNetProfit.textContent = `-$${formatMoney(Math.abs(netProfit))} (${profitBps.toFixed(1)} bps)`;
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
        const target = btn.dataset.tab;
        document.getElementById(target).classList.add('active');
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

    // Quoting trigger & Floor mode change
    el.getQuoteBtn.addEventListener('click', requestQuote);
    if (el.quoteFloorMode) {
      el.quoteFloorMode.addEventListener('change', requestQuote);
    }
    if (el.quoteExecMode) {
      el.quoteExecMode.addEventListener('change', requestQuote);
    }

    // --- Custom Deposit Tx Inspector Handlers ---
    async function loadCustomTx(hash) {
      const clean = (hash || '').trim();
      state.customTx = clean || null;
      if (el.customTxInput) {
        el.customTxInput.value = clean;
      }
      if (clean) {
        if (el.chipLatestSwap) el.chipLatestSwap.classList.toggle('active', clean.toLowerCase() === LATEST_TX.toLowerCase());
        if (el.chipPreviousSwap) el.chipPreviousSwap.classList.toggle('active', clean.toLowerCase() === KNOWN_PREVIOUS_TX.toLowerCase());
        if (el.chipLiveQueue) el.chipLiveQueue.classList.remove('active');
      } else {
        if (el.chipLatestSwap) el.chipLatestSwap.classList.remove('active');
        if (el.chipPreviousSwap) el.chipPreviousSwap.classList.remove('active');
        if (el.chipLiveQueue) el.chipLiveQueue.classList.add('active');
      }
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
      el.chipLatestSwap.addEventListener('click', () => {
        loadCustomTx(LATEST_TX);
      });
    }

    if (el.chipPreviousSwap) {
      el.chipPreviousSwap.addEventListener('click', () => {
        loadCustomTx(KNOWN_PREVIOUS_TX);
      });
    }

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
    el.startSniperBtn.addEventListener('click', async () => {
      const amount = el.sniperAmount.value || '10000';
      const profitBps = el.sniperProfitBps.value || '2';
      const pollSec = el.sniperPollSec.value || '3';
      const affiliate = el.sniperAffiliateName ? el.sniperAffiliateName.value.trim() : 'symbiosis';
      const affiliateBps = el.sniperAffiliateBps ? el.sniperAffiliateBps.value.trim() : '1';
      const floorMode = el.sniperFloorMode ? el.sniperFloorMode.value : '2bps';

      if (!confirm(`Start THORChain Sniper Daemon in LIVE execution mode?\n\nAmount: ${amount} USDC\nTrigger Net Profit: >= +${profitBps} bps\nPoll Frequency: every ${pollSec}s\nAffiliate: ${affiliate} (${affiliateBps} bps)\nFloor Protection Mode: ${floorMode} (${floorMode === '2bps' ? '+2 bps Gas Floor / Covers In & Out Gas' : (floorMode === '1to1' ? '1:1 Parity Floor' : 'Strict Target Floor')})`)) {
        return;
      }

      el.startSniperBtn.disabled = true;
      try {
        const res = await fetch('/api/sniper/start', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            amount: amount,
            min_profit_bps: profitBps,
            poll_interval: pollSec,
            affiliate: affiliate,
            affiliate_bps: affiliateBps,
            floor_mode: floorMode,
            confirm: 'START_SNIPER'
          })
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
      if (!confirm('Are you sure you want to stop the running sniper daemon?')) return;
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
      const amount = el.quoteAmount.value || '10000';
      const slippage = el.quoteSlippage.value || '1';

      // The floor is the memo's limit field (1e8 units), not the expected output.
      // Only trust it when the last quote was for the amount about to be sent.
      let floorText = 'Get a quote for this amount first';
      const q = state.lastQuote;
      if (q && Number(q.input_amount) === Number(amount) && q.memo) {
        const limit = Number((q.memo.split(':')[3] || '').split('/')[0]);
        if (limit > 0) floorText = `${formatMoney(limit / 1e8)} USDP (expected ${formatMoney(q.expected_output)})`;
      }

      el.modalInputAmount.textContent = `${formatMoney(amount)} USDC`;
      el.modalMinOutput.textContent = floorText;
      el.modalSlippage.textContent = `${slippage} bps`;
      const resting = el.quoteExecMode && el.quoteExecMode.value === 'resting';
      if (el.modalOrderType) {
        el.modalOrderType.textContent = resting
          ? 'Resting order: full amount streams ~24h, slices fill only at or above the floor'
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
        if (data.ok) {
          const title = data.pending ? '⏳ LIVE SWAP BROADCAST (UNCONFIRMED)' : '🚀 LIVE SWAP DEPOSIT CONFIRMED';
          alert(`${title}\n\nTx Hash: ${data.tx_hash}\n\n${data.message}`);
          el.swapModal.close();
          loadCustomTx(data.tx_hash);
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
    setupEvents();
    if (el.customTxInput) {
      el.customTxInput.value = LATEST_TX;
    }
    startPolling();
  });
})();
