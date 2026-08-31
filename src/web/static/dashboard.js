(() => {
  "use strict";

  const $ = (id) => document.getElementById(id);
  const elements = {
    connection: $("connection-badge"),
    systemStatus: $("system-status"),
    metricStatus: $("metric-status"),
    metricMode: $("metric-mode"),
    metricChecks: $("metric-checks"),
    metricRouteCount: $("metric-route-count"),
    metricBestProfit: $("metric-best-profit"),
    metricBestRoute: $("metric-best-route"),
    metricConfirmed: $("metric-confirmed"),
    metricOutcomes: $("metric-outcomes"),
    sessionMode: $("session-mode"),
    sessionCallout: $("session-callout"),
    sessionLabel: $("session-label"),
    sessionGuidance: $("session-guidance"),
    sessionPid: $("session-pid"),
    sessionUptime: $("session-uptime"),
    sessionUpdated: $("session-updated"),
    sessionChains: $("session-chains"),
    activeChainGrid: $("active-chain-grid"),
    feedStatus: $("feed-status"),
    summaryReady: $("summary-ready"),
    summaryNoTrade: $("summary-no-trade"),
    summaryPaused: $("summary-paused"),
    summarySubmitted: $("summary-submitted"),
    lastExecutionCard: $("last-execution-card"),
    lastExecutionTitle: $("last-execution-title"),
    lastExecutionDetail: $("last-execution-detail"),
    lastExecutionLink: $("last-execution-link"),
    routeTableBody: $("route-table-body"),
    routeTableNote: $("route-table-note"),
    terminal: $("terminal"),
    copyLogsButton: $("copy-logs-button"),
    copyUrlButton: $("copy-url-button"),
    copyUrlFeedback: $("copy-url-feedback"),
    webappAddress: $("webapp-address"),
  };

  let currentLogs = [];
  let logFilter = "all";
  let pollTimer = null;

  function setText(element, value) {
    if (element) element.textContent = value;
  }

  function parseDate(value) {
    const date = value ? new Date(value) : null;
    return date && Number.isFinite(date.getTime()) ? date : null;
  }

  function formatDuration(totalSeconds) {
    if (!Number.isFinite(totalSeconds) || totalSeconds < 0) return "—";
    const seconds = Math.floor(totalSeconds);
    const hours = Math.floor(seconds / 3600);
    const minutes = Math.floor((seconds % 3600) / 60);
    const remainder = seconds % 60;
    if (hours) return `${hours}h ${minutes}m ${remainder}s`;
    if (minutes) return `${minutes}m ${remainder}s`;
    return `${remainder}s`;
  }

  function relativeTime(value) {
    const date = parseDate(value);
    if (!date) return "—";
    const seconds = Math.max(0, Math.round((Date.now() - date.getTime()) / 1000));
    if (seconds < 5) return "just now";
    if (seconds < 60) return `${seconds}s ago`;
    if (seconds < 3600) return `${Math.floor(seconds / 60)}m ago`;
    return `${Math.floor(seconds / 3600)}h ago`;
  }

  function compactNumber(value) {
    const number = Number(value || 0);
    return Number.isFinite(number) ? number.toLocaleString() : "0";
  }

  function profitText(value, token) {
    if (value === null || value === undefined || value === "") return "—";
    const number = Number(value);
    if (!Number.isFinite(number)) return "—";
    const decimals = Math.abs(number) >= 100 ? 2 : 6;
    return `${number.toLocaleString(undefined, { maximumFractionDigits: decimals })} ${token || ""}`.trim();
  }

  function routeValues(routes) {
    if (!routes || typeof routes !== "object") return [];
    return Array.isArray(routes) ? routes : Object.values(routes);
  }

  function bestRoute(routes) {
    return routes.reduce((best, route) => {
      const net = Number(route.net_profit);
      if (!Number.isFinite(net)) return best;
      if (!best || net > Number(best.net_profit)) return route;
      return best;
    }, null);
  }

  function renderSession(session, summary, routes) {
    const running = Boolean(session.running);
    const mode = session.mode === "live" ? "LIVE" : session.mode === "dry-run" ? "DRY RUN" : "OFFLINE";
    const statusLabel = session.status_label || (running ? "Sniper running" : "Sniper is not running");
    const started = parseDate(session.started_at);

    elements.connection.dataset.state = running ? "online" : "offline";
    setText(elements.systemStatus, running ? "Sniper online" : "Sniper offline");
    setText(elements.metricStatus, running ? "Running" : "Offline");
    setText(elements.metricMode, running ? `${mode} · PID ${session.pid}` : "Run start_sniper.cmd to begin");
    setText(elements.sessionMode, mode);
    elements.sessionMode.dataset.state = running ? (session.mode === "live" ? "live" : "ready") : "offline";
    elements.sessionCallout.dataset.state = running ? "online" : "offline";
    setText(elements.sessionLabel, statusLabel);
    setText(
      elements.sessionGuidance,
      running
        ? "Live status is read directly from the active sniper process."
        : "Start it from a separate terminal to populate this dashboard.",
    );
    setText(elements.sessionPid, running ? String(session.pid) : "—");
    setText(elements.sessionUptime, running && started ? formatDuration((Date.now() - started.getTime()) / 1000) : "—");
    setText(elements.sessionUpdated, relativeTime(session.updated_at));
    setText(elements.sessionChains, Array.isArray(session.chains) && session.chains.length ? session.chains.map((chain) => chain[0].toUpperCase() + chain.slice(1)).join(" + ") : "—");

    setText(elements.metricChecks, compactNumber(summary.checks));
    setText(elements.metricRouteCount, `${session.route_count || routes.length} configured routes`);
    setText(elements.metricConfirmed, `${summary.confirmed || 0} confirmed`);
    setText(elements.metricOutcomes, `${summary.ready || 0} ready · ${summary.errors || 0} errors`);
    setText(elements.summaryReady, compactNumber(summary.ready));
    setText(elements.summaryNoTrade, compactNumber(summary.no_trade));
    setText(elements.summaryPaused, compactNumber(summary.paused));
    setText(elements.summarySubmitted, compactNumber(summary.submitted));
    elements.feedStatus.dataset.state = running ? "success" : "idle";
    setText(elements.feedStatus, running ? "Live feed" : "Offline");

    const best = bestRoute(routes);
    setText(elements.metricBestProfit, best ? profitText(best.net_profit, best.profit_token) : "—");
    setText(elements.metricBestRoute, best ? `${best.chain} · ${best.pair} · ${best.swap_order}` : "No completed checks");
  }

  function renderActive(active) {
    elements.activeChainGrid.replaceChildren();
    const checks = Object.entries(active || {}).filter(([, route]) => route);
    if (!checks.length) {
      const empty = document.createElement("p");
      empty.className = "empty-inline";
      empty.textContent = "No route checks are active.";
      elements.activeChainGrid.append(empty);
      return;
    }
    checks.forEach(([chain, route]) => {
      const card = document.createElement("article");
      card.className = "active-check";
      const header = document.createElement("div");
      const name = document.createElement("strong");
      name.textContent = chain[0].toUpperCase() + chain.slice(1);
      const badge = document.createElement("span");
      badge.textContent = "Checking";
      header.append(name, badge);
      const flow = document.createElement("p");
      flow.textContent = route.flow || route.pair;
      const elapsed = document.createElement("small");
      const start = parseDate(route.started_at);
      elapsed.textContent = `Running ${start ? formatDuration((Date.now() - start.getTime()) / 1000) : "now"} · required ${profitText(route.execution_floor, route.profit_token)}`;
      card.append(header, flow, elapsed);
      elements.activeChainGrid.append(card);
    });
  }

  function statusRank(state) {
    return { CHECKING: 0, CONFIRMED: 1, STOPPED: 2, READY: 3, ERROR: 4, REVERTED: 5, PAUSED: 6, "NO TRADE": 7, DROPPED: 8, WAITING: 9 }[state] ?? 10;
  }

  function appendCell(row, text, className = "") {
    const cell = document.createElement("td");
    if (className) cell.className = className;
    cell.textContent = text;
    row.append(cell);
    return cell;
  }

  function renderRoutes(routes) {
    elements.routeTableBody.replaceChildren();
    if (!routes.length) {
      const row = document.createElement("tr");
      const cell = appendCell(row, "No sniper session has been recorded yet.", "empty-table");
      cell.colSpan = 6;
      elements.routeTableBody.append(row);
      setText(elements.routeTableNote, "Waiting for start_sniper.cmd");
      return;
    }
    const sorted = [...routes].sort((left, right) => {
      const rank = statusRank(left.state) - statusRank(right.state);
      if (rank) return rank;
      return String(right.checked_at || "").localeCompare(String(left.checked_at || ""));
    });
    sorted.forEach((route) => {
      const row = document.createElement("tr");
      row.dataset.state = String(route.state || "waiting").toLowerCase().replace(" ", "-");
      row.title = route.cooldown_reason || route.detail || "";

      const stateCell = document.createElement("td");
      const status = document.createElement("span");
      status.className = "route-status";
      status.textContent = route.state || "WAITING";
      stateCell.append(status);
      row.append(stateCell);

      const routeCell = document.createElement("td");
      routeCell.className = "route-name";
      const chain = document.createElement("span");
      chain.textContent = `${route.chain || "—"} · ${route.swap_order || "—"}`;
      const flow = document.createElement("strong");
      flow.textContent = route.flow || route.pair || "—";
      const detail = document.createElement("small");
      detail.textContent = route.cooldown_reason || route.detail || "Waiting for check";
      routeCell.append(chain, flow, detail);
      row.append(routeCell);

      appendCell(row, profitText(route.gross_profit, route.profit_token), "profit-cell");
      const netCell = appendCell(row, profitText(route.net_profit, route.profit_token), "profit-cell net-cell");
      const net = Number(route.net_profit);
      const floor = Number(route.execution_floor);
      if (Number.isFinite(net) && Number.isFinite(floor)) netCell.dataset.result = net >= floor ? "positive" : "negative";
      appendCell(row, profitText(route.execution_floor, route.profit_token), "profit-cell");
      appendCell(row, relativeTime(route.checked_at), "time-cell");
      elements.routeTableBody.append(row);
    });
    setText(elements.routeTableNote, `${routes.length} route variants · newest actionable results first`);
  }

  function safeExplorerUrl(value) {
    try {
      const url = new URL(value);
      if (url.protocol !== "https:") return null;
      if (!["etherscan.io", "solscan.io"].includes(url.hostname)) return null;
      return url.href;
    } catch (_error) {
      return null;
    }
  }

  function renderExecution(execution) {
    if (!execution) {
      elements.lastExecutionCard.dataset.state = "empty";
      setText(elements.lastExecutionTitle, "No transaction submitted this session");
      setText(elements.lastExecutionDetail, "Confirmed, reverted, dropped, and unresolved transactions will appear here.");
      elements.lastExecutionLink.hidden = true;
      return;
    }
    elements.lastExecutionCard.dataset.state = String(execution.state || "result").toLowerCase();
    setText(elements.lastExecutionTitle, `${execution.state || "RESULT"} · ${execution.chain} · ${execution.pair}`);
    setText(elements.lastExecutionDetail, execution.detail || execution.flow || "Transaction result recorded.");
    const url = safeExplorerUrl(execution.transaction);
    if (url) {
      elements.lastExecutionLink.href = url;
      elements.lastExecutionLink.hidden = false;
    } else {
      elements.lastExecutionLink.hidden = true;
    }
  }

  function renderState(state) {
    const session = state.session || {};
    const summary = state.summary || {};
    const routes = routeValues(state.routes);
    renderSession(session, summary, routes);
    renderActive(state.active || {});
    renderRoutes(routes);
    renderExecution(state.last_execution);
  }

  function filteredLogs() {
    return logFilter === "all" ? currentLogs : currentLogs.filter((entry) => entry.type === logFilter);
  }

  function renderLogs() {
    const entries = filteredLogs();
    const nearBottom = elements.terminal.scrollHeight - elements.terminal.scrollTop - elements.terminal.clientHeight < 60;
    elements.terminal.replaceChildren();
    if (!entries.length) {
      const empty = document.createElement("div");
      empty.className = "empty-log";
      empty.textContent = currentLogs.length ? "No log lines match this filter." : "No sniper log has been written yet.";
      elements.terminal.append(empty);
      return;
    }
    entries.forEach((entry) => {
      const line = document.createElement("div");
      line.className = `log-line log-${entry.type || "info"}`;
      const timestamp = document.createElement("span");
      timestamp.className = "log-timestamp";
      timestamp.textContent = String(entry.timestamp || "").replace(/^\d{4}-\d{2}-\d{2}\s*/, "");
      const text = document.createElement("span");
      text.className = "log-text";
      text.textContent = entry.text || "";
      line.append(timestamp, text);
      elements.terminal.append(line);
    });
    if (nearBottom) elements.terminal.scrollTop = elements.terminal.scrollHeight;
  }

  async function fetchJson(path) {
    const controller = new AbortController();
    const timeout = window.setTimeout(() => controller.abort(), 5000);
    try {
      const response = await fetch(path, { cache: "no-store", signal: controller.signal });
      if (!response.ok) throw new Error(`${path} returned ${response.status}`);
      return await response.json();
    } finally {
      window.clearTimeout(timeout);
    }
  }

  async function refresh() {
    try {
      const [state, logState] = await Promise.all([fetchJson("/api/state"), fetchJson("/api/logs")]);
      renderState(state);
      currentLogs = Array.isArray(logState.logs) ? logState.logs : [];
      renderLogs();
    } catch (_error) {
      elements.connection.dataset.state = "error";
      setText(elements.systemStatus, "Dashboard unavailable");
      setText(elements.feedStatus, "Feed error");
      elements.feedStatus.dataset.state = "error";
    } finally {
      pollTimer = window.setTimeout(refresh, 2000);
    }
  }

  document.querySelectorAll("[data-log-filter]").forEach((button) => {
    button.addEventListener("click", () => {
      logFilter = button.dataset.logFilter || "all";
      document.querySelectorAll("[data-log-filter]").forEach((candidate) => {
        const active = candidate === button;
        candidate.classList.toggle("is-active", active);
        candidate.setAttribute("aria-pressed", String(active));
      });
      renderLogs();
    });
  });

  elements.copyLogsButton.addEventListener("click", async () => {
    const text = filteredLogs().map((entry) => `${entry.timestamp || ""} ${entry.text || ""}`.trim()).join("\n");
    try {
      await navigator.clipboard.writeText(text);
      setText(elements.copyLogsButton, "Copied");
    } catch (_error) {
      setText(elements.copyLogsButton, "Copy failed");
    }
    window.setTimeout(() => setText(elements.copyLogsButton, "Copy log"), 1200);
  });

  elements.webappAddress.textContent = window.location.host;
  elements.copyUrlButton.addEventListener("click", async () => {
    try {
      await navigator.clipboard.writeText(window.location.href);
      setText(elements.copyUrlFeedback, "Copied");
    } catch (_error) {
      setText(elements.copyUrlFeedback, "Failed");
    }
    window.setTimeout(() => setText(elements.copyUrlFeedback, "Copy"), 1200);
  });

  window.addEventListener("beforeunload", () => {
    if (pollTimer) window.clearTimeout(pollTimer);
  });
  refresh();
})();
