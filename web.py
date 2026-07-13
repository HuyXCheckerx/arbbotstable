import json
import os
from datetime import datetime, timezone
from http.server import HTTPServer, BaseHTTPRequestHandler

from state_store import DEFAULT_STATE_PATH, read_state


PORT = int(os.environ.get("PORT", 25284))
STATE_PATH = os.environ.get("BOT_STATE_FILE", str(DEFAULT_STATE_PATH))

HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <meta name="color-scheme" content="dark">
  <meta name="theme-color" content="#080b0b">
  <title>Stable Execution &mdash; Arbitrage Operations</title>
  <style>
    :root {
      color-scheme: dark;
      --canvas: #080b0b;
      --surface: #0d1110;
      --surface-raised: #111614;
      --surface-inset: #0a0e0d;
      --line: rgba(223, 220, 207, .12);
      --line-soft: rgba(223, 220, 207, .075);
      --line-metal: rgba(204, 177, 112, .34);
      --text: #efeee8;
      --text-soft: #b3b6b0;
      --muted: #767e79;
      --metal: #c7aa69;
      --metal-bright: #e1ca94;
      --green: #79b493;
      --red: #d57c75;
      --amber: #d0a75e;
      --radius: 2px;
      --sans: "Aptos", "Segoe UI Variable", "Segoe UI", Helvetica, Arial, sans-serif;
      --serif: "Iowan Old Style", Baskerville, "Palatino Linotype", Georgia, serif;
      --mono: "SFMono-Regular", "Cascadia Mono", Consolas, monospace;
    }

    * { box-sizing: border-box; }
    html { min-width: 320px; background: var(--canvas); }
    body {
      margin: 0;
      min-height: 100vh;
      background: var(--canvas);
      color: var(--text);
      font-family: var(--sans);
      font-variant-numeric: tabular-nums;
      -webkit-font-smoothing: antialiased;
      text-rendering: optimizeLegibility;
    }

    body::before {
      content: "";
      position: fixed;
      inset: 0 0 auto;
      z-index: 10;
      height: 2px;
      background: var(--metal);
      opacity: .78;
    }

    .shell { width: min(1460px, calc(100% - 64px)); margin: 0 auto; padding: 34px 0 48px; }
    .masthead {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 32px;
      min-height: 62px;
      padding-bottom: 24px;
      margin-bottom: 24px;
      border-bottom: 1px solid var(--line);
    }
    .brand-lockup { display: flex; align-items: center; gap: 16px; min-width: 0; }
    .monogram {
      position: relative;
      width: 42px;
      height: 42px;
      flex: 0 0 auto;
      display: grid;
      place-items: center;
      border: 1px solid var(--line-metal);
      color: var(--metal-bright);
      font: 600 10px/1 var(--sans);
      letter-spacing: .16em;
      text-indent: .16em;
    }
    .monogram::after {
      content: "";
      position: absolute;
      right: -1px;
      bottom: -1px;
      width: 8px;
      height: 8px;
      border-right: 1px solid var(--metal);
      border-bottom: 1px solid var(--metal);
    }
    .brand-overline,
    .meta-label,
    .section-code,
    .section-label,
    .eyebrow {
      color: var(--muted);
      font-size: 9px;
      font-weight: 600;
      line-height: 1.3;
      letter-spacing: .16em;
      text-transform: uppercase;
    }
    .brand-overline { margin-bottom: 5px; color: var(--metal); }
    .brand-copy h1 { margin: 0; font-family: var(--serif); font-size: clamp(20px, 2vw, 25px); font-weight: 400; line-height: 1.05; letter-spacing: .015em; }
    .header-meta { display: flex; align-items: stretch; gap: 0; flex: 0 0 auto; }
    .live-meta,
    .sync-meta { min-height: 42px; display: flex; flex-direction: column; justify-content: center; }
    .live-meta { padding-right: 24px; border-right: 1px solid var(--line); }
    .sync-meta { min-width: 166px; padding-left: 24px; align-items: flex-end; }
    .status-pill { display: inline-flex; align-items: center; gap: 10px; color: var(--text); font-size: 11px; font-weight: 600; letter-spacing: .02em; }
    #updated-at { margin-top: 5px; color: var(--text-soft); font-family: var(--mono); font-size: 10px; }
    .dot { width: 6px; height: 6px; border-radius: 50%; background: var(--muted); box-shadow: 0 0 0 3px rgba(118, 126, 121, .1); }
    .dot.good { background: var(--green); }
    .dot.warn { background: var(--amber); }
    .dot.bad { background: var(--red); }

    .offline-banner { display: none; margin: 0 0 18px; padding: 12px 15px; border: 1px solid rgba(213, 124, 117, .28); border-left: 2px solid var(--red); background: #15100f; color: #e0a19c; font-size: 11px; line-height: 1.5; }
    .overview {
      display: grid;
      grid-template-columns: minmax(420px, 1.15fr) minmax(650px, 1fr);
      margin-bottom: 18px;
      border: 1px solid var(--line);
      border-top-color: var(--line-metal);
      border-radius: var(--radius);
      background: var(--surface);
      box-shadow: 0 18px 56px rgba(0, 0, 0, .19);
      overflow: hidden;
    }
    .overview-primary { min-height: 238px; padding: 30px 34px 28px; border-right: 1px solid var(--line); background: var(--surface-inset); }
    .overview-caption { display: flex; align-items: center; justify-content: space-between; gap: 20px; margin-bottom: 33px; }
    .ledger-mark { color: var(--metal); font-size: 9px; font-weight: 600; letter-spacing: .13em; text-transform: uppercase; }
    .overview-primary .eyebrow { margin-bottom: 10px; }
    .pnl { font-family: var(--serif); font-size: clamp(48px, 5.6vw, 76px); line-height: .92; font-weight: 400; letter-spacing: -.045em; white-space: nowrap; }
    .positive { color: var(--green) !important; }
    .negative { color: var(--red) !important; }
    .pnl-breakdown { display: flex; align-items: center; flex-wrap: wrap; gap: 15px 28px; margin-top: 27px; }
    .breakdown-item { min-width: 130px; }
    .breakdown-item + .breakdown-item { padding-left: 28px; border-left: 1px solid var(--line); }
    .breakdown-item span { display: block; margin-bottom: 6px; color: var(--muted); font-size: 9px; letter-spacing: .08em; text-transform: uppercase; }
    .breakdown-item strong { font-family: var(--mono); font-size: 12px; font-weight: 500; }
    .overview-metrics { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); }
    .summary-metric { min-width: 0; padding: 31px 23px 25px; border-left: 1px solid var(--line); }
    .summary-metric:first-child { border-left: 0; }
    .summary-metric .eyebrow { min-height: 25px; margin-bottom: 28px; }
    .metric { min-width: 0; overflow: hidden; color: var(--text); font-family: var(--serif); font-size: clamp(27px, 2.4vw, 35px); font-weight: 400; line-height: 1; letter-spacing: -.035em; text-overflow: ellipsis; white-space: nowrap; }
    .metric-mono { font-family: var(--mono); font-size: clamp(20px, 2vw, 27px); letter-spacing: -.045em; }
    .subvalue { margin-top: 17px; color: var(--muted); font-size: 10px; line-height: 1.5; }
    .subvalue span { color: var(--text-soft); }

    .workspace { display: grid; grid-template-columns: minmax(0, 1.65fr) minmax(330px, .62fr); align-items: start; gap: 18px; }
    .panel { min-width: 0; border: 1px solid var(--line); border-radius: var(--radius); background: var(--surface); box-shadow: 0 14px 44px rgba(0, 0, 0, .13); overflow: hidden; }
    .panel-head { min-height: 69px; padding: 17px 22px; display: flex; align-items: center; justify-content: space-between; gap: 20px; border-bottom: 1px solid var(--line); background: var(--surface-raised); }
    .panel-heading { min-width: 0; }
    .section-code { margin-bottom: 6px; color: var(--metal); }
    .panel-title { margin: 0; font-family: var(--serif); font-size: 17px; font-weight: 400; line-height: 1.2; letter-spacing: .01em; }
    .panel-note { color: var(--muted); font-size: 9px; line-height: 1.4; letter-spacing: .08em; text-align: right; text-transform: uppercase; }
    .token-grid { display: grid; grid-template-columns: repeat(4, 1fr); }
    .token { min-width: 0; padding: 26px 21px 25px; border-right: 1px solid var(--line); }
    .token:last-child { border-right: 0; }
    .token-name { display: flex; align-items: center; gap: 9px; color: var(--text-soft); font-size: 9px; font-weight: 650; letter-spacing: .12em; }
    .token-icon { width: 7px; height: 7px; border: 1px solid var(--metal); transform: rotate(45deg); opacity: .78; }
    .token-amount { margin-top: 22px; overflow: hidden; font-family: var(--mono); font-size: clamp(18px, 1.8vw, 24px); font-weight: 450; line-height: 1.15; letter-spacing: -.055em; text-overflow: ellipsis; white-space: nowrap; }
    .token-usd { margin-top: 10px; color: var(--muted); font-family: var(--mono); font-size: 10px; }
    .pool-list { display: grid; grid-template-columns: repeat(3, 1fr); }
    .pool { min-width: 0; padding: 20px 22px 22px; border-right: 1px solid var(--line); background: var(--surface-inset); }
    .pool:last-child { border-right: 0; }
    .pool strong { display: block; margin-top: 10px; overflow: hidden; font-family: var(--mono); font-size: 16px; font-weight: 450; letter-spacing: -.035em; text-overflow: ellipsis; white-space: nowrap; }

    .execution { padding: 22px; }
    .route { position: relative; margin-bottom: 18px; padding: 18px 18px 17px; border: 1px solid var(--line); border-left-color: var(--line-metal); background: var(--surface-inset); overflow: hidden; }
    .route::before { content: ""; position: absolute; inset: 0 auto 0 0; width: 2px; background: var(--metal); opacity: .65; }
    .route .eyebrow { margin-bottom: 9px; }
    .route-value { color: var(--metal-bright); font-family: var(--serif); font-size: 19px; font-weight: 400; line-height: 1.25; word-break: break-word; }
    .detail-list { display: grid; gap: 0; }
    .detail { display: flex; justify-content: space-between; align-items: baseline; gap: 18px; min-height: 43px; padding: 12px 0; border-bottom: 1px solid var(--line-soft); }
    .detail:last-child { border-bottom: 0; }
    .detail span { color: var(--muted); font-size: 10px; }
    .detail strong { max-width: 62%; overflow: hidden; font-family: var(--mono); font-size: 10px; font-weight: 500; text-align: right; text-overflow: ellipsis; white-space: nowrap; }
    .error { display: none; margin-top: 17px; padding: 12px 14px; border: 1px solid rgba(213, 124, 117, .24); border-left: 2px solid var(--red); background: #15100f; color: #dda09b; font-family: var(--mono); font-size: 10px; line-height: 1.55; word-break: break-word; }

    .activity { grid-column: 1 / -1; }
    .table-wrap { overflow-x: auto; }
    table { width: 100%; min-width: 930px; border-collapse: collapse; }
    th { padding: 13px 18px; border-bottom: 1px solid var(--line); background: var(--surface-inset); color: var(--muted); font-size: 8px; font-weight: 650; letter-spacing: .12em; text-align: left; text-transform: uppercase; white-space: nowrap; }
    td { padding: 16px 18px; border-top: 1px solid var(--line-soft); color: var(--text-soft); font-family: var(--mono); font-size: 10px; white-space: nowrap; }
    td:nth-child(3) { max-width: 280px; overflow: hidden; color: var(--text); font-family: var(--sans); font-size: 11px; text-overflow: ellipsis; }
    tbody tr { transition: background-color 120ms ease; }
    tbody tr:hover { background: rgba(255, 255, 255, .018); }
    .badge { display: inline-flex; align-items: center; gap: 7px; color: var(--green); font-family: var(--sans); font-size: 8px; font-weight: 700; letter-spacing: .1em; text-transform: uppercase; }
    .badge::before { content: ""; width: 5px; height: 5px; border-radius: 50%; background: currentColor; }
    .badge.failed { color: var(--red); }
    .empty { padding: 44px 20px; color: var(--muted); font-family: var(--serif); font-size: 14px; text-align: center; }
    footer { display: flex; justify-content: space-between; gap: 32px; padding-top: 20px; margin-top: 22px; border-top: 1px solid var(--line); color: var(--muted); font-size: 9px; line-height: 1.7; letter-spacing: .02em; }
    footer p { margin: 0; max-width: 760px; }
    .footer-mark { flex: 0 0 auto; color: var(--text-soft); font-family: var(--mono); text-align: right; text-transform: uppercase; }

    @media (max-width: 1160px) {
      .overview { grid-template-columns: 1fr; }
      .overview-primary { min-height: auto; border-right: 0; border-bottom: 1px solid var(--line); }
      .summary-metric { min-height: 152px; }
      .workspace { grid-template-columns: 1fr; }
      .activity { grid-column: auto; }
    }

    @media (max-width: 760px) {
      .shell { width: min(100% - 28px, 1460px); padding-top: 23px; }
      .masthead { align-items: flex-start; gap: 22px; }
      .sync-meta { display: none; }
      .live-meta { padding: 4px 0 0; border-right: 0; }
      .overview-primary { padding: 25px 22px 23px; }
      .overview-caption { margin-bottom: 27px; }
      .pnl { font-size: clamp(42px, 12vw, 66px); }
      .overview-metrics { grid-template-columns: 1fr 1fr; }
      .summary-metric { min-height: 135px; padding: 23px 21px 21px; border-bottom: 1px solid var(--line); }
      .summary-metric:nth-child(3) { grid-column: 1 / -1; border-left: 0; border-bottom: 0; }
      .summary-metric .eyebrow { min-height: auto; margin-bottom: 20px; }
      .panel-head { padding: 16px 18px; }
      .token-grid { grid-template-columns: 1fr 1fr; }
      .token:nth-child(2) { border-right: 0; }
      .token:nth-child(-n+2) { border-bottom: 1px solid var(--line); }
      .execution { padding: 18px; }
      footer { flex-direction: column; }
      .footer-mark { text-align: left; }
    }

    @media (max-width: 500px) {
      .monogram { width: 37px; height: 37px; }
      .brand-copy h1 { font-size: 19px; }
      .brand-overline { font-size: 8px; }
      .header-meta { align-self: center; }
      .live-meta .meta-label { display: none; }
      .status-pill { max-width: 88px; gap: 7px; font-size: 9px; }
      #status-label { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
      .dot { width: 7px; height: 7px; }
      .overview-primary { padding-inline: 18px; }
      .pnl-breakdown { gap: 15px 20px; }
      .breakdown-item + .breakdown-item { padding-left: 20px; }
      .token { padding: 22px 17px; }
      .pool-list { grid-template-columns: 1fr; }
      .pool { border-right: 0; border-bottom: 1px solid var(--line); }
      .pool:last-child { border-bottom: 0; }
      .panel-head > .panel-note { display: none; }
    }

    @media (prefers-reduced-motion: reduce) {
      *, *::before, *::after { scroll-behavior: auto !important; transition: none !important; }
    }
  </style>
</head>
<body>
  <main class="shell">
    <header class="masthead">
      <div class="brand-lockup">
        <div class="monogram" aria-hidden="true">SE</div>
        <div class="brand-copy">
          <div class="brand-overline">Stable Execution</div>
          <h1>Arbitrage Operations</h1>
        </div>
      </div>
      <div class="header-meta">
        <div class="live-meta">
          <span class="meta-label">System state</span>
          <div class="status-pill" aria-live="polite"><span id="status-dot" class="dot"></span><span id="status-label">Connecting</span></div>
        </div>
        <div class="sync-meta">
          <span class="meta-label">Last synchronization</span>
          <span id="updated-at">Waiting for data</span>
        </div>
      </div>
    </header>

    <div id="offline-banner" class="offline-banner" role="status">Live state is unavailable. The dashboard will keep retrying.</div>

    <section class="overview" aria-label="Portfolio performance">
      <div class="overview-primary">
        <div class="overview-caption"><span class="section-label">Performance ledger</span><span class="ledger-mark">Realized</span></div>
        <div class="eyebrow">Net profit and loss</div>
        <div id="total-pnl" class="pnl">$0.00</div>
        <div class="pnl-breakdown">
          <div class="breakdown-item"><span>Current session</span><strong id="session-pnl">$0.00</strong></div>
          <div class="breakdown-item"><span>Prior-method estimate</span><strong id="legacy-pnl">$0.00</strong></div>
        </div>
      </div>
      <div class="overview-metrics">
        <div class="summary-metric">
          <div class="eyebrow">Successful arbs</div>
          <div id="total-arbs" class="metric">0</div>
          <div class="subvalue">Session <span id="session-arbs">0</span></div>
        </div>
        <div class="summary-metric">
          <div class="eyebrow">Wallet value</div>
          <div id="portfolio" class="metric">$0.00</div>
          <div class="subvalue">Stablecoins marked at $1</div>
        </div>
        <div class="summary-metric">
          <div class="eyebrow">Observed SOL spent</div>
          <div id="sol-spent" class="metric metric-mono">0.000000</div>
          <div class="subvalue"><span id="sol-cost">~$0.00</span> at execution prices</div>
        </div>
      </div>
    </section>

    <div class="workspace">
      <section class="panel" aria-labelledby="wallet-heading">
        <div class="panel-head">
          <div class="panel-heading"><div class="section-code">Positions / 01</div><h2 id="wallet-heading" class="panel-title">Wallet balances</h2></div>
          <span class="panel-note">Confirmed RPC state</span>
        </div>
        <div id="token-grid" class="token-grid"></div>
        <div class="panel-head">
          <div class="panel-heading"><div class="section-code">Liquidity / 02</div><h2 class="panel-title">Stable.com reserves</h2></div>
          <span class="panel-note">Latest observed balances</span>
        </div>
        <div id="pool-list" class="pool-list"></div>
      </section>

      <aside class="panel" aria-labelledby="execution-heading">
        <div class="panel-head">
          <div class="panel-heading"><div class="section-code">Runtime / 03</div><h2 id="execution-heading" class="panel-title">Execution state</h2></div>
          <span id="uptime" class="panel-note">00:00:00</span>
        </div>
        <div class="execution">
          <div class="route"><div class="eyebrow">Current route</div><div id="current-route" class="route-value">Market scan</div></div>
          <div class="detail-list">
            <div class="detail"><span>Bot status</span><strong id="detail-status">Offline</strong></div>
            <div class="detail"><span>Session attempts</span><strong id="attempts">0</strong></div>
            <div class="detail"><span>Session SOL cost</span><strong id="session-sol-cost">$0.00</strong></div>
            <div class="detail"><span>SOL reference price</span><strong id="sol-price">$0.00</strong></div>
            <div class="detail"><span>Wallet</span><strong id="wallet">&mdash;</strong></div>
          </div>
          <div id="last-error" class="error" role="status"></div>
        </div>
      </aside>

      <section class="panel activity" aria-labelledby="ledger-heading">
        <div class="panel-head">
          <div class="panel-heading"><div class="section-code">Ledger / 04</div><h2 id="ledger-heading" class="panel-title">Execution history</h2></div>
          <span class="panel-note">Successful and failed attempts</span>
        </div>
        <div class="table-wrap">
          <table>
            <thead><tr><th>Time</th><th>Status</th><th>Route</th><th>Size</th><th>Expected gross</th><th>Stable &Delta;</th><th>SOL spent</th><th>Net P&amp;L</th></tr></thead>
            <tbody id="activity-body"></tbody>
          </table>
          <div id="activity-empty" class="empty">No attempts recorded in the persistent ledger yet.</div>
        </div>
      </section>
    </div>

    <footer>
      <p>Net P&amp;L = change in USDC + USDG + PYUSD balances, valued at $1 each, minus the USD estimate of the wallet's observed SOL decrease during each attempt. This avoids treating ordinary SOL price movement as arbitrage profit.</p>
      <p class="footer-mark">Private operations ledger<br>2-second refresh</p>
    </footer>
  </main>

  <script>
    const $ = (id) => document.getElementById(id);
    const money = (value, digits = 2) => new Intl.NumberFormat('en-US', { style: 'currency', currency: 'USD', minimumFractionDigits: digits, maximumFractionDigits: digits }).format(Number(value || 0));
    const number = (value, digits = 6) => new Intl.NumberFormat('en-US', { minimumFractionDigits: 0, maximumFractionDigits: digits }).format(Number(value || 0));
    const duration = (seconds) => { seconds = Math.max(0, Number(seconds || 0)); const h = Math.floor(seconds / 3600); const m = Math.floor((seconds % 3600) / 60); const s = Math.floor(seconds % 60); return [h,m,s].map(v => String(v).padStart(2,'0')).join(':'); };
    const text = (id, value) => { $(id).textContent = value; };
    const pnlTone = (element, value) => { element.classList.remove('positive','negative'); element.classList.add(Number(value) >= 0 ? 'positive' : 'negative'); };
    const shortWallet = (wallet) => wallet && wallet.length > 14 ? `${wallet.slice(0,6)}…${wallet.slice(-6)}` : (wallet || '—');

    function renderAssets(wallet, pools) {
      const tokenGrid = $('token-grid');
      tokenGrid.replaceChildren();
      ['USDC','USDG','PYUSD','SOL'].forEach(asset => {
        const data = wallet[asset] || {};
        const node = document.createElement('div');
        node.className = 'token';
        const name = document.createElement('div'); name.className = 'token-name';
        const icon = document.createElement('span'); icon.className = 'token-icon';
        name.append(icon, document.createTextNode(asset));
        const amount = document.createElement('div'); amount.className = 'token-amount'; amount.textContent = number(data.amount, asset === 'SOL' ? 9 : 6);
        const usd = document.createElement('div'); usd.className = 'token-usd'; usd.textContent = money(data.usd_value, 2);
        node.append(name, amount, usd); tokenGrid.append(node);
      });

      const poolList = $('pool-list'); poolList.replaceChildren();
      ['USDC','USDG','PYUSD'].forEach(asset => {
        const node = document.createElement('div'); node.className = 'pool';
        const label = document.createElement('div'); label.className = 'panel-note'; label.textContent = asset + ' available';
        const value = document.createElement('strong'); value.textContent = number((pools[asset] || {}).amount, 2);
        node.append(label, value); poolList.append(node);
      });
    }

    function renderActivity(records) {
      const body = $('activity-body'); body.replaceChildren();
      $('activity-empty').style.display = records.length ? 'none' : 'block';
      records.slice(0, 12).forEach(record => {
        const row = document.createElement('tr');
        const values = [
          new Date(record.timestamp).toLocaleString([], { month:'short', day:'numeric', hour:'2-digit', minute:'2-digit', second:'2-digit' }),
          record.status,
          record.route,
          number(record.amount, 2),
          money(record.expected_gross_profit_usd, 4),
          money(record.stablecoin_change_usd, 4),
          number(record.sol_consumed, 9) + ' SOL',
          money(record.realized_pnl_usd, 4),
        ];
        values.forEach((value, index) => {
          const cell = document.createElement('td');
          if (index === 1) { const badge = document.createElement('span'); badge.className = 'badge' + (record.status === 'failed' ? ' failed' : ''); badge.textContent = value; cell.append(badge); }
          else { cell.textContent = value; }
          if (index === 7) cell.className = Number(record.realized_pnl_usd) >= 0 ? 'positive' : 'negative';
          row.append(cell);
        });
        body.append(row);
      });
    }

    function render(state) {
      const bot = state.bot || {}, performance = state.performance || {}, balances = state.balances || {}, market = state.market || {};
      text('total-pnl', money(performance.total_realized_pnl_usd, 2)); pnlTone($('total-pnl'), performance.total_realized_pnl_usd);
      text('session-pnl', money(performance.session_realized_pnl_usd, 4)); pnlTone($('session-pnl'), performance.session_realized_pnl_usd);
      text('legacy-pnl', money(performance.legacy_balance_change_usd, 4));
      text('total-arbs', number(performance.total_arbs, 0)); text('session-arbs', number(performance.session_arbs, 0));
      text('portfolio', money(performance.current_portfolio_usd, 2));
      text('sol-spent', number(performance.total_sol_consumed, 9)); text('sol-cost', '~' + money(performance.total_sol_cost_usd, 4));
      text('status-label', bot.status_label || 'Unknown'); text('detail-status', bot.status_label || 'Unknown');
      text('updated-at', bot.updated_at ? 'Updated ' + new Date(bot.updated_at).toLocaleTimeString() : 'Waiting for data');
      text('uptime', duration(bot.uptime_seconds)); text('current-route', bot.current_route || 'Market scan');
      text('attempts', number(performance.session_attempts, 0)); text('session-sol-cost', money(performance.session_sol_cost_usd, 4));
      text('sol-price', money(market.sol_usd, 2)); text('wallet', shortWallet(bot.wallet)); $('wallet').title = bot.wallet || '';
      const dot = $('status-dot'); dot.className = 'dot ' + (['scanning','executing_stable','executing_jupiter'].includes(bot.status) ? 'good' : (['starting','recovering','exposed'].includes(bot.status) ? 'warn' : 'bad'));
      const error = $('last-error'); error.textContent = bot.last_error || ''; error.style.display = bot.last_error ? 'block' : 'none';
      renderAssets(balances.wallet || {}, balances.pools || {}); renderActivity(state.recent_arbs || []);
      const updateAge = bot.updated_at ? (Date.now() - new Date(bot.updated_at).getTime()) / 1000 : Infinity;
      if (updateAge > 30) {
        $('offline-banner').textContent = 'Bot state is stale. The dashboard is reachable, but the trading process may be stopped.';
        $('offline-banner').style.display = 'block';
        dot.className = 'dot bad'; text('status-label', 'Stale');
      } else {
        $('offline-banner').style.display = 'none';
      }
    }

    async function refresh() {
      try {
        const response = await fetch('/api/state', { cache: 'no-store' });
        if (!response.ok) throw new Error('State endpoint unavailable');
        render(await response.json());
      } catch (error) {
        $('offline-banner').style.display = 'block';
        $('status-dot').className = 'dot bad'; text('status-label', 'Disconnected');
      }
    }
    refresh(); setInterval(refresh, 2000);
  </script>
</body>
</html>"""


def state_is_fresh(state, max_age_seconds=30):
    updated_at = state.get("bot", {}).get("updated_at")
    if not updated_at:
        return False
    try:
        updated = datetime.fromisoformat(updated_at.replace("Z", "+00:00"))
        return (datetime.now(timezone.utc) - updated).total_seconds() <= max_age_seconds
    except ValueError:
        return False


class DashboardHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass

    def _send(self, status, content_type, payload):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path == "/api/state":
            payload = json.dumps(read_state(STATE_PATH), separators=(",", ":")).encode("utf-8")
            self._send(200, "application/json; charset=utf-8", payload)
            return
        if path == "/healthz":
            state = read_state(STATE_PATH)
            healthy = state_is_fresh(state) and state.get("bot", {}).get("status") != "offline"
            payload = json.dumps({"ok": healthy, "status": state.get("bot", {}).get("status", "offline")}).encode("utf-8")
            self._send(200 if healthy else 503, "application/json; charset=utf-8", payload)
            return
        if path in ("/", "/index.html"):
            self._send(200, "text/html; charset=utf-8", HTML_TEMPLATE.encode("utf-8"))
            return
        self._send(404, "application/json; charset=utf-8", b'{"error":"not found"}')


def run():
    server_address = ("0.0.0.0", PORT)
    httpd = HTTPServer(server_address, DashboardHandler)
    print(f"[*] Dashboard running on port {PORT}")
    httpd.serve_forever()


if __name__ == "__main__":
    run()
