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
  <title>Stable Execution — Portfolio &amp; P&amp;L</title>
  <style>
    :root {
      --bg: #0b0b0b;
      --panel: #11110f;
      --panel-soft: #0e0e0d;
      --line: #2c2b27;
      --line-soft: #211f1c;
      --text: #e9e5dc;
      --muted: #8e8a81;
      --accent: #b9a36a;
      --green: #70a988;
      --red: #c97871;
      --amber: #c3a25f;
      --radius: 3px;
    }

    * { box-sizing: border-box; }
    body {
      margin: 0;
      min-height: 100vh;
      background: var(--bg);
      color: var(--text);
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Helvetica, Arial, sans-serif;
      font-variant-numeric: tabular-nums;
      -webkit-font-smoothing: antialiased;
    }

    .shell { width: min(1380px, calc(100% - 64px)); margin: 0 auto; padding: 42px 0 56px; }
    header { display: flex; align-items: flex-end; justify-content: space-between; gap: 24px; padding-bottom: 22px; border-bottom: 1px solid var(--line); margin-bottom: 32px; }
    .brand { display: flex; align-items: center; gap: 17px; }
    .brand-mark { width: 28px; height: 28px; border: 1px solid var(--accent); border-radius: 0; display: grid; place-items: center; color: var(--accent); font: 600 9px/1 ui-monospace, "SFMono-Regular", monospace; }
    .brand h1 { margin: 0; font-family: "Iowan Old Style", "Baskerville", Georgia, serif; font-size: 22px; font-weight: 400; letter-spacing: .025em; }
    .brand p { margin: 5px 0 0; color: var(--muted); font-size: 10px; letter-spacing: .1em; text-transform: uppercase; }
    .live-meta { display: flex; align-items: center; gap: 16px; color: var(--muted); font-size: 11px; }
    .status-pill { display: inline-flex; align-items: center; gap: 9px; padding: 0 16px 0 0; border: 0; border-right: 1px solid var(--line); border-radius: 0; background: none; color: var(--text); }
    .dot { width: 5px; height: 5px; border-radius: 50%; background: var(--muted); }
    .dot.good { background: var(--green); }
    .dot.warn { background: var(--amber); }
    .dot.bad { background: var(--red); }

    .hero { display: grid; grid-template-columns: minmax(0, 1.6fr) repeat(3, minmax(170px, .62fr)); border-top: 1px solid var(--line); border-bottom: 1px solid var(--line); background: transparent; overflow: hidden; margin-bottom: 24px; }
    .hero > div { padding: 30px 26px 28px; min-height: 154px; border-left: 1px solid var(--line); }
    .hero > div:first-child { border-left: 0; }
    .eyebrow { color: var(--muted); font-size: 10px; text-transform: uppercase; letter-spacing: .12em; margin-bottom: 20px; }
    .pnl { font-family: "Iowan Old Style", "Baskerville", Georgia, serif; font-size: clamp(40px, 4.5vw, 64px); line-height: .95; font-weight: 400; letter-spacing: -.035em; }
    .positive { color: var(--green) !important; }
    .negative { color: var(--red) !important; }
    .subvalue { margin-top: 14px; color: var(--muted); font-size: 11px; }
    .metric { font-family: "Iowan Old Style", "Baskerville", Georgia, serif; font-size: 30px; font-weight: 400; letter-spacing: -.025em; color: var(--text); }

    .grid { display: grid; grid-template-columns: minmax(0, 1.55fr) minmax(340px, .68fr); gap: 24px; }
    .panel { border: 1px solid var(--line); background: var(--panel); border-radius: var(--radius); overflow: hidden; }
    .panel-head { min-height: 57px; padding: 17px 20px; display: flex; align-items: center; justify-content: space-between; gap: 16px; border-bottom: 1px solid var(--line); }
    .panel-title { margin: 0; font-family: "Iowan Old Style", "Baskerville", Georgia, serif; font-size: 16px; font-weight: 400; letter-spacing: .01em; }
    .panel-note { color: var(--muted); font-size: 10px; letter-spacing: .04em; }
    .token-grid { display: grid; grid-template-columns: repeat(4, 1fr); }
    .token { padding: 24px 20px 26px; border-right: 1px solid var(--line); }
    .token:last-child { border-right: 0; }
    .token-name { display: flex; align-items: center; gap: 9px; color: var(--muted); font-size: 10px; font-weight: 500; letter-spacing: .08em; }
    .token-icon { width: 10px; height: 1px; border-radius: 0; background: var(--accent); }
    .token:nth-child(n) .token-icon { background: var(--accent); }
    .token-amount { margin-top: 17px; font-family: "Iowan Old Style", "Baskerville", Georgia, serif; font-size: clamp(20px, 2.2vw, 28px); font-weight: 400; letter-spacing: -.025em; word-break: break-word; }
    .token-usd { margin-top: 8px; color: var(--muted); font-size: 11px; }
    .pool-list { display: grid; grid-template-columns: repeat(3, 1fr); }
    .pool { padding: 18px 20px 20px; border-right: 1px solid var(--line); }
    .pool:last-child { border-right: 0; }
    .pool strong { display: block; margin-top: 9px; font-family: "Iowan Old Style", "Baskerville", Georgia, serif; font-size: 18px; font-weight: 400; }

    .execution { padding: 22px 20px 20px; }
    .route { margin-bottom: 20px; padding: 0 0 20px; background: none; border: 0; border-bottom: 1px solid var(--line); border-radius: 0; }
    .route-value { margin-top: 8px; font-family: "Iowan Old Style", "Baskerville", Georgia, serif; font-size: 18px; font-weight: 400; color: var(--accent); word-break: break-word; }
    .detail-list { display: grid; gap: 0; }
    .detail { display: flex; justify-content: space-between; align-items: baseline; gap: 18px; padding: 13px 0; border-bottom: 1px solid var(--line-soft); }
    .detail:last-child { border-bottom: 0; }
    .detail span { color: var(--muted); font-size: 11px; }
    .detail strong { font-size: 12px; font-weight: 500; text-align: right; }
    .error { display: none; margin-top: 16px; padding: 12px 14px; border-left: 2px solid var(--red); border-radius: 0; background: #17100f; color: #d9948e; font-size: 11px; line-height: 1.5; }

    .activity { grid-column: 1 / -1; }
    .table-wrap { overflow-x: auto; }
    table { width: 100%; border-collapse: collapse; min-width: 880px; }
    th { padding: 14px 20px; color: var(--muted); font-size: 9px; font-weight: 500; letter-spacing: .1em; text-transform: uppercase; text-align: left; background: var(--panel-soft); border-bottom: 1px solid var(--line); }
    td { padding: 16px 20px; border-top: 1px solid var(--line-soft); font-size: 11px; }
    tbody tr:hover { background: #141310; }
    .badge { display: inline-flex; align-items: center; padding: 0; border-radius: 0; font-size: 9px; font-weight: 600; text-transform: uppercase; letter-spacing: .08em; background: none; color: var(--green); }
    .badge.failed { color: var(--red); background: none; }
    .empty { padding: 38px 20px; color: var(--muted); font-family: "Iowan Old Style", "Baskerville", Georgia, serif; font-size: 14px; text-align: center; }
    footer { display: flex; justify-content: space-between; gap: 24px; padding-top: 20px; margin-top: 24px; border-top: 1px solid var(--line); color: var(--muted); font-size: 10px; line-height: 1.65; }
    footer p { margin: 0; max-width: 760px; }
    .offline-banner { display: none; margin-bottom: 24px; padding: 12px 0 12px 14px; border: 0; border-left: 2px solid var(--red); background: #17100f; color: #d9948e; border-radius: 0; font-size: 11px; }

    @media (max-width: 1040px) {
      .hero { grid-template-columns: repeat(3, 1fr); }
      .hero-main { grid-column: 1 / -1; border-bottom: 1px solid var(--line); }
      .hero > div:nth-child(2) { border-left: 0; }
      .grid { grid-template-columns: 1fr; }
      .activity { grid-column: auto; }
    }
    @media (max-width: 720px) {
      .shell { width: min(100% - 28px, 1380px); padding-top: 24px; }
      header { align-items: flex-start; }
      .live-meta > span:last-child { display: none; }
      .hero { grid-template-columns: 1fr 1fr; }
      .hero-main { grid-column: 1 / -1; }
      .hero > div { padding: 21px 19px; min-height: 120px; }
      .hero > div:nth-child(4) { grid-column: 1 / -1; border-left: 0; border-top: 1px solid var(--line); }
      .token-grid { grid-template-columns: 1fr 1fr; }
      .token:nth-child(2) { border-right: 0; }
      .token:nth-child(-n+2) { border-bottom: 1px solid var(--line); }
      .pool-list { grid-template-columns: 1fr; }
      .pool { border-right: 0; border-bottom: 1px solid var(--line); }
      footer { flex-direction: column; }
    }
  </style>
</head>
<body>
  <main class="shell">
    <header>
      <div class="brand">
        <div class="brand-mark">S·E</div>
        <div><h1>Stable Execution</h1><p>Private portfolio and execution ledger</p></div>
      </div>
      <div class="live-meta">
        <div class="status-pill"><span id="status-dot" class="dot"></span><span id="status-label">Connecting</span></div>
        <span id="updated-at">Waiting for data</span>
      </div>
    </header>

    <div id="offline-banner" class="offline-banner">Live state is unavailable. The dashboard will keep retrying.</div>

    <section class="hero">
      <div class="hero-main">
        <div class="eyebrow">Realized net P&amp;L</div>
        <div id="total-pnl" class="pnl">$0.00</div>
        <div class="subvalue">Session <span id="session-pnl">$0.00</span> · prior-method estimate <span id="legacy-pnl">$0.00</span></div>
      </div>
      <div><div class="eyebrow">Successful arbs</div><div id="total-arbs" class="metric">0</div><div class="subvalue">Session <span id="session-arbs">0</span></div></div>
      <div><div class="eyebrow">Wallet value</div><div id="portfolio" class="metric">$0.00</div><div class="subvalue">Stablecoins valued at $1</div></div>
      <div><div class="eyebrow">Observed SOL spent</div><div id="sol-spent" class="metric">0.000000</div><div class="subvalue"><span id="sol-cost">~$0.00</span> at execution prices</div></div>
    </section>

    <div class="grid">
      <section class="panel">
        <div class="panel-head"><h2 class="panel-title">Wallet balances</h2><span class="panel-note">Confirmed RPC state</span></div>
        <div id="token-grid" class="token-grid"></div>
        <div class="panel-head"><h2 class="panel-title">Stable.com pool liquidity</h2><span class="panel-note">Latest observed balances</span></div>
        <div id="pool-list" class="pool-list"></div>
      </section>

      <aside class="panel">
        <div class="panel-head"><h2 class="panel-title">Execution state</h2><span id="uptime" class="panel-note">00:00:00</span></div>
        <div class="execution">
          <div class="route"><div class="eyebrow">Current route</div><div id="current-route" class="route-value">Market scan</div></div>
          <div class="detail-list">
            <div class="detail"><span>Bot status</span><strong id="detail-status">Offline</strong></div>
            <div class="detail"><span>Session attempts</span><strong id="attempts">0</strong></div>
            <div class="detail"><span>Session SOL cost</span><strong id="session-sol-cost">$0.00</strong></div>
            <div class="detail"><span>SOL reference price</span><strong id="sol-price">$0.00</strong></div>
            <div class="detail"><span>Wallet</span><strong id="wallet">—</strong></div>
          </div>
          <div id="last-error" class="error"></div>
        </div>
      </aside>

      <section class="panel activity">
        <div class="panel-head"><h2 class="panel-title">Execution ledger</h2><span class="panel-note">Successful and failed attempts</span></div>
        <div class="table-wrap">
          <table>
            <thead><tr><th>Time</th><th>Status</th><th>Route</th><th>Size</th><th>Expected gross</th><th>Stable Δ</th><th>SOL spent</th><th>Net P&amp;L</th></tr></thead>
            <tbody id="activity-body"></tbody>
          </table>
          <div id="activity-empty" class="empty">No attempts recorded in the persistent ledger yet.</div>
        </div>
      </section>
    </div>

    <footer>
      <p>Net P&amp;L = change in USDC + USDG + PYUSD balances, valued at $1 each, minus the USD estimate of the wallet's observed SOL decrease during each attempt. This avoids treating ordinary SOL price movement as arbitrage profit.</p>
      <p>Live ledger · 2-second refresh</p>
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
