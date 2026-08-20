"use strict";

const LIVE_CONFIRMATION = "EXECUTE LIVE ARB";
const REFRESH_INTERVAL_MS = 3000;
const REFRESH_TIMEOUT_MS = 8000;
const SUPPORTED_PAIRS = {
  ethereum: ["PYUSD/USDC", "USDT/USDC", "USDG/PYUSD", "PYUSD/USDG"],
  solana: ["PYUSD/USDC", "USDT/USDC", "USDG/PYUSD", "PYUSD/USDG"],
  polygon: ["PYUSD/USDC"],
  bsc: ["USDT/USDC"],
};

const elements = {
  form: document.getElementById("execution-form"),
  chainButtons: [...document.querySelectorAll("[data-chain]")],
  amountPresets: [...document.querySelectorAll("[data-amount]")],
  logFilters: [...document.querySelectorAll("[data-log-filter]")],
  pair: document.getElementById("pair-select"),
  pairNote: document.getElementById("pair-note"),
  amount: document.getElementById("amount-input"),
  slippage: document.getElementById("slippage-select"),
  quoteButton: document.getElementById("quote-button"),
  liveButton: document.getElementById("live-button"),
  formError: document.getElementById("form-error"),
  routeChip: document.getElementById("route-chip"),
  connection: document.getElementById("connection-badge"),
  systemStatus: document.getElementById("system-status"),
  quoteStatus: document.getElementById("quote-status"),
  quoteFlow: document.getElementById("quote-flow"),
  quoteLoan: document.getElementById("quote-loan"),
  quoteCost: document.getElementById("quote-cost"),
  quoteLeg1: document.getElementById("quote-leg1"),
  quoteLeg2: document.getElementById("quote-leg2"),
  quoteGross: document.getElementById("quote-gross"),
  quoteNet: document.getElementById("quote-net"),
  terminal: document.getElementById("terminal"),
  botStatus: document.getElementById("metric-bot-status"),
  route: document.getElementById("metric-route"),
  pyusd: document.getElementById("metric-pyusd"),
  usdt: document.getElementById("metric-usdt"),
  pnl: document.getElementById("metric-pnl"),
  attempts: document.getElementById("metric-attempts"),
  liveDialog: document.getElementById("live-dialog"),
  confirmationForm: document.getElementById("live-confirmation-form"),
  confirmationInput: document.getElementById("confirmation-input"),
  confirmationError: document.getElementById("confirmation-error"),
  confirmationSummary: document.getElementById("confirmation-summary"),
  copyUrlButton: document.getElementById("copy-url-button"),
  copyUrlFeedback: document.getElementById("copy-url-feedback"),
  webappAddress: document.getElementById("webapp-address"),
  copyLogsButton: document.getElementById("copy-logs-button"),
};

let selectedChain = "ethereum";
let requestRunning = false;
let lastLogFingerprint = "";
let lastLogs = [];
let activeLogFilter = "all";

function routeSymbols() {
  const [intermediate, loan] = elements.pair.value.split("/");
  return { intermediate, loan };
}

function dexName() {
  return selectedChain === "solana" ? "Jupiter" : "Matcha";
}

function updateRoutePreview() {
  const { intermediate, loan } = routeSymbols();
  elements.quoteFlow.textContent = `${loan} → ${intermediate} (${dexName()}) → ${loan} (Stable.com)`;
  elements.quoteLoan.textContent = `${elements.amount.value || "—"} ${loan}`;
  const zeroFee = intermediate !== "USDT";
  elements.routeChip.textContent = zeroFee ? "0-fee route" : "standard route";
  elements.pairNote.textContent = zeroFee
    ? `${dexName()} entry with Stable.com par settlement.`
    : `${dexName()} entry with Stable.com standard USDT settlement.`;
  for (const button of elements.amountPresets) {
    button.classList.toggle("is-active", button.dataset.amount === elements.amount.value);
  }
  saveSettings();
}

function selectChain(chain) {
  selectedChain = chain;
  for (const button of elements.chainButtons) {
    const active = button.dataset.chain === chain;
    button.classList.toggle("is-active", active);
    button.setAttribute("aria-pressed", String(active));
  }

  const supported = SUPPORTED_PAIRS[chain];
  for (const option of elements.pair.options) {
    option.disabled = !supported.includes(option.value);
  }
  if (!supported.includes(elements.pair.value)) {
    elements.pair.value = supported[0];
  }
  updateRoutePreview();
}

function setConnection(state, label) {
  elements.connection.dataset.state = state;
  elements.systemStatus.textContent = label;
}

function setQuoteStatus(state, label) {
  elements.quoteStatus.dataset.state = state;
  elements.quoteStatus.textContent = label;
}

function setBusy(busy) {
  requestRunning = busy;
  elements.quoteButton.disabled = busy;
  elements.liveButton.disabled = busy;
  for (const button of elements.chainButtons) {
    button.disabled = busy;
  }
  elements.pair.disabled = busy;
  elements.amount.disabled = busy;
  elements.slippage.disabled = busy;
  for (const button of elements.amountPresets) {
    button.disabled = busy;
  }
  if (busy) {
    setConnection("busy", "Execution running");
  }
}

function validateForm() {
  const amount = elements.amount.value.trim();
  if (!/^\d+(?:\.\d+)?$/.test(amount) || Number(amount) <= 0) {
    elements.formError.textContent = "Enter a positive decimal principal.";
    elements.amount.focus();
    return false;
  }
  elements.formError.textContent = "";
  return true;
}

function requestPayload(mode, confirmation = "") {
  return {
    chain: selectedChain,
    pair: elements.pair.value,
    mode,
    amount: elements.amount.value.trim(),
    slippageBps: elements.slippage.value,
    confirmation,
  };
}

function renderQuote(parsed) {
  if (!parsed || typeof parsed !== "object") return;
  const assignments = [
    [elements.quoteFlow, parsed.flow],
    [elements.quoteLoan, parsed.loan],
    [elements.quoteCost, parsed.cost],
    [elements.quoteLeg1, parsed.leg1],
    [elements.quoteLeg2, parsed.leg2],
    [elements.quoteGross, parsed.gross],
    [elements.quoteNet, parsed.net],
  ];
  for (const [element, value] of assignments) {
    if (value) element.textContent = value;
  }
}

async function runArbitrage(mode, confirmation = "") {
  setBusy(true);
  setQuoteStatus("loading", mode === "live" ? "Executing live" : "Requesting quote");
  elements.formError.textContent = "";

  try {
    const response = await fetch("/api/run", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(requestPayload(mode, confirmation)),
    });
    const data = await response.json();
    renderQuote(data.parsed);
    if (!data.ok) {
      throw new Error(data.error || `Request failed with HTTP ${response.status}`);
    }
    setQuoteStatus("success", mode === "live" ? "Transaction submitted" : "Quote ready");
  } catch (error) {
    const message = error instanceof Error ? error.message : "Request failed";
    elements.formError.textContent = message;
    setQuoteStatus("error", "Request failed");
  } finally {
    setBusy(false);
    await refreshOnce();
  }
}

function openLiveConfirmation() {
  const { intermediate, loan } = routeSymbols();
  elements.confirmationInput.value = "";
  elements.confirmationError.textContent = "";
  elements.confirmationSummary.textContent = `${selectedChain.toUpperCase()} · ${elements.amount.value} ${loan} · ${loan} → ${intermediate} → ${loan}`;
  elements.liveDialog.showModal();
  elements.confirmationInput.focus();
}

elements.form.addEventListener("submit", (event) => {
  event.preventDefault();
  if (requestRunning || !validateForm()) return;
  const mode = event.submitter?.dataset.mode || "quote";
  if (mode === "live") {
    openLiveConfirmation();
  } else {
    void runArbitrage("quote");
  }
});

elements.confirmationForm.addEventListener("submit", (event) => {
  event.preventDefault();
  const action = event.submitter?.value;
  if (action === "cancel") {
    elements.liveDialog.close();
    return;
  }
  if (elements.confirmationInput.value !== LIVE_CONFIRMATION) {
    elements.confirmationError.textContent = `Type ${LIVE_CONFIRMATION} exactly.`;
    elements.confirmationInput.focus();
    return;
  }
  elements.liveDialog.close();
  void runArbitrage("live", LIVE_CONFIRMATION);
});

for (const button of elements.chainButtons) {
  button.addEventListener("click", () => selectChain(button.dataset.chain));
}
elements.pair.addEventListener("change", updateRoutePreview);
elements.amount.addEventListener("input", updateRoutePreview);
elements.slippage.addEventListener("change", saveSettings);
for (const button of elements.amountPresets) {
  button.addEventListener("click", () => {
    elements.amount.value = button.dataset.amount;
    updateRoutePreview();
  });
}

function saveSettings() {
  try {
    localStorage.setItem(
      "arb-dashboard-settings",
      JSON.stringify({
        chain: selectedChain,
        pair: elements.pair.value,
        amount: elements.amount.value,
        slippage: elements.slippage.value,
      }),
    );
  } catch (_error) {
    // The dashboard remains fully usable when storage is unavailable.
  }
}

function restoreSettings() {
  try {
    const saved = JSON.parse(localStorage.getItem("arb-dashboard-settings") || "null");
    if (!saved || typeof saved !== "object") return;
    if (SUPPORTED_PAIRS[saved.chain]) selectedChain = saved.chain;
    if (typeof saved.amount === "string" && /^\d+(?:\.\d+)?$/.test(saved.amount)) {
      elements.amount.value = saved.amount;
    }
    if ([...elements.slippage.options].some((option) => option.value === saved.slippage)) {
      elements.slippage.value = saved.slippage;
    }
    selectChain(selectedChain);
    if (SUPPORTED_PAIRS[selectedChain].includes(saved.pair)) {
      elements.pair.value = saved.pair;
    }
  } catch (_error) {
    // Ignore corrupt or blocked local storage.
  }
}

function filteredLogs() {
  if (activeLogFilter === "error") {
    return lastLogs.filter((item) => item?.type === "error");
  }
  if (activeLogFilter === "quote") {
    return lastLogs.filter((item) => item?.type === "quote" || item?.type === "success");
  }
  return lastLogs;
}

function renderLogs(logs) {
  if (!Array.isArray(logs)) return;
  lastLogs = logs;
  const visibleLogs = filteredLogs();
  const fingerprint = `${activeLogFilter}:${JSON.stringify(visibleLogs)}`;
  if (fingerprint === lastLogFingerprint) return;
  lastLogFingerprint = fingerprint;

  const shouldStick =
    elements.terminal.scrollHeight - elements.terminal.scrollTop - elements.terminal.clientHeight < 45;
  const fragment = document.createDocumentFragment();
  for (const item of visibleLogs) {
    if (!item || typeof item !== "object") continue;
    const line = document.createElement("div");
    const type = ["info", "success", "error", "quote", "system"].includes(item.type)
      ? item.type
      : "info";
    line.className = `log-line log-${type}`;

    const timestamp = document.createElement("span");
    timestamp.className = "log-timestamp";
    timestamp.textContent = `[${String(item.timestamp || "--:--:--")}]`;

    const text = document.createElement("span");
    text.className = "log-text";
    text.textContent = String(item.text || "");

    line.append(timestamp, text);
    fragment.appendChild(line);
  }
  if (!fragment.childNodes.length) {
    const empty = document.createElement("div");
    empty.className = "empty-log";
    empty.textContent = `No ${activeLogFilter === "all" ? "" : `${activeLogFilter} `}entries to show.`;
    fragment.appendChild(empty);
  }
  elements.terminal.replaceChildren(fragment);
  if (shouldStick || !elements.terminal.dataset.rendered) {
    elements.terminal.scrollTop = elements.terminal.scrollHeight;
  }
  elements.terminal.dataset.rendered = "true";
}

for (const button of elements.logFilters) {
  button.addEventListener("click", () => {
    activeLogFilter = button.dataset.logFilter;
    for (const candidate of elements.logFilters) {
      const active = candidate === button;
      candidate.classList.toggle("is-active", active);
      candidate.setAttribute("aria-pressed", String(active));
    }
    lastLogFingerprint = "";
    renderLogs(lastLogs);
  });
}

async function copyText(value, feedback, successLabel) {
  const original = feedback.textContent;
  try {
    if (navigator.clipboard?.writeText) {
      await navigator.clipboard.writeText(value);
    } else {
      throw new Error("Clipboard API unavailable");
    }
    feedback.textContent = successLabel;
  } catch (_error) {
    const fallback = document.createElement("textarea");
    fallback.value = value;
    fallback.setAttribute("readonly", "");
    fallback.style.position = "fixed";
    fallback.style.opacity = "0";
    document.body.appendChild(fallback);
    fallback.select();
    const copied = document.execCommand("copy");
    fallback.remove();
    feedback.textContent = copied ? successLabel : "Copy failed";
  }
  window.setTimeout(() => {
    feedback.textContent = original;
  }, 1400);
}

elements.webappAddress.textContent = window.location.host;
elements.copyUrlButton.addEventListener("click", () => {
  void copyText(window.location.href, elements.copyUrlFeedback, "URL copied");
});
elements.copyLogsButton.addEventListener("click", () => {
  const text = filteredLogs()
    .map((item) => `[${item.timestamp || "--:--:--"}] ${item.text || ""}`)
    .join("\n");
  void copyText(text || "No log entries.", elements.copyLogsButton, "Copied");
});

const numberFormatter = new Intl.NumberFormat(undefined, {
  maximumFractionDigits: 2,
});
const pnlFormatter = new Intl.NumberFormat(undefined, {
  style: "currency",
  currency: "USD",
  minimumFractionDigits: 2,
  maximumFractionDigits: 2,
  signDisplay: "exceptZero",
});

function poolAmount(state, symbol) {
  const value = state?.balances?.pools?.[symbol]?.amount;
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

function renderState(state) {
  const bot = state?.bot || {};
  const performance = state?.performance || {};
  const offline = bot.status === "offline";
  elements.botStatus.textContent = bot.status_label || bot.status || "Unknown";
  elements.route.textContent = bot.current_route || "No active route";

  const pyusd = poolAmount(state, "PYUSD");
  const usdt = poolAmount(state, "USDT");
  elements.pyusd.textContent = offline || pyusd === null ? "—" : `${numberFormatter.format(pyusd)} PYUSD`;
  elements.usdt.textContent = offline || usdt === null ? "—" : `${numberFormatter.format(usdt)} USDT`;

  const pnl = Number(performance.session_realized_pnl_usd || 0);
  elements.pnl.textContent = pnlFormatter.format(pnl);
  elements.pnl.style.color = pnl > 0 ? "var(--green)" : pnl < 0 ? "var(--red)" : "var(--text)";
  const attempts = Number(performance.session_attempts || 0);
  elements.attempts.textContent = `${attempts} ${attempts === 1 ? "attempt" : "attempts"}`;
}

async function fetchJson(url, signal) {
  const response = await fetch(url, { cache: "no-store", signal });
  if (!response.ok) throw new Error(`${url} returned HTTP ${response.status}`);
  return response.json();
}

async function refreshOnce() {
  const controller = new AbortController();
  const timeout = window.setTimeout(() => controller.abort(), REFRESH_TIMEOUT_MS);
  try {
    const [logsResult, stateResult] = await Promise.allSettled([
      fetchJson("/api/logs", controller.signal),
      fetchJson("/api/state", controller.signal),
    ]);

    let consoleReachable = false;
    let executionRunning = requestRunning;
    if (logsResult.status === "fulfilled") {
      consoleReachable = true;
      executionRunning ||= Boolean(logsResult.value.running);
      renderLogs(logsResult.value.logs);
    }
    if (stateResult.status === "fulfilled") {
      consoleReachable = true;
      renderState(stateResult.value);
    }

    if (executionRunning) {
      setConnection("busy", "Execution running");
    } else if (consoleReachable) {
      setConnection("online", "Console online");
    } else {
      setConnection("error", "Disconnected");
    }
  } catch (_error) {
    setConnection("error", "Disconnected");
  } finally {
    window.clearTimeout(timeout);
  }
}

async function refreshLoop() {
  await refreshOnce();
  window.setTimeout(refreshLoop, REFRESH_INTERVAL_MS);
}

restoreSettings();
selectChain(selectedChain);
updateRoutePreview();
void refreshLoop();
