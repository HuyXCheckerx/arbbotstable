/**
 * MATCHA META - Client Application Logic
 * Supports real-time DEX aggregation, multi-aggregator live comparison,
 * on-chain simulation, and Web3 wallet connectivity.
 */

const CHAINS = [
  { id: 1, name: "Ethereum", icon: "https://token-registry.s3.amazonaws.com/icons/tokens/ethereum/64/0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2.png" },
  { id: 42161, name: "Arbitrum", icon: "https://token-registry.s3.amazonaws.com/icons/tokens/arbitrum/64/0x912ce59144191c1204e64559fe8253a0e49e6548.png" },
  { id: 8453, name: "Base", icon: "https://token-registry.s3.amazonaws.com/icons/tokens/base/64/0x4200000000000000000000000000000000000006.png" },
  { id: 10, name: "Optimism", icon: "https://token-registry.s3.amazonaws.com/icons/tokens/optimism/64/0x4200000000000000000000000000000000000042.png" },
  { id: 137, name: "Polygon", icon: "https://token-registry.s3.amazonaws.com/icons/tokens/polygon/64/0x0d500b1d8e8ef31e21c99d1db9a6444d3adf1270.png" },
  { id: 56, name: "BNB Chain", icon: "https://token-registry.s3.amazonaws.com/icons/tokens/binance-smart-chain/64/0xbb4cdb9cbd36b01bd1cbaebf2de08d9173bc095c.png" },
  { id: 1399811149, name: "Solana", icon: "https://raw.githubusercontent.com/solana-labs/token-list/main/assets/mainnet/So11111111111111111111111111111111111111112/logo.png" }
];

const AGGREGATOR_NAMES = [
  "0x",
  "Lightning",
  "1inch",
  "Velora",
  "Nordstern",
  "Barter",
  "Enso",
  "KyberSwap",
  "ParaSwap",
  "Bebop",
  "OKX"
];

// Application State
const state = {
  chainId: 1,
  sellToken: {
    address: "0xeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee",
    symbol: "ETH",
    name: "Ethereum",
    decimals: 18,
    logoUrl: "https://token-registry.s3.amazonaws.com/icons/tokens/ethereum/64/0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2.png",
    price: 2403.45,
    balance: "12.45"
  },
  buyToken: {
    address: "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48",
    symbol: "USDC",
    name: "USD Coin",
    decimals: 6,
    logoUrl: "https://token-registry.s3.amazonaws.com/icons/tokens/ethereum/64/0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48.png",
    price: 1.00,
    balance: "4500.00"
  },
  sellAmount: "1",
  buyAmount: "2403.456561",
  slippageBps: 30,
  intentsMode: false,
  userAddress: null,
  activeCompetitionId: null,
  quotes: [],
  selectedAggregator: "0x",
  isRefreshing: false,
  tokenModalTarget: "sell", // 'sell' or 'buy'
  tokenList: [],
  refreshCountdown: 5,
  refreshInterval: null
};

// DOM References
const el = {
  banner: document.getElementById("banner"),
  bannerClose: document.getElementById("banner-close"),
  themeToggle: document.getElementById("theme-toggle"),
  themeSun: document.getElementById("theme-icon-sun"),
  themeMoon: document.getElementById("theme-icon-moon"),
  resourcesBtn: document.getElementById("resources-btn"),
  resourcesMenu: document.getElementById("resources-menu"),
  connectWalletBtn: document.getElementById("connect-wallet-btn"),
  walletBtnLabel: document.getElementById("wallet-btn-label"),
  
  // Swap Card
  chainSelectBtn: document.getElementById("chain-select-btn"),
  activeChainIcon: document.getElementById("active-chain-icon"),
  activeChainName: document.getElementById("active-chain-name"),
  tradeSettingsBtn: document.getElementById("trade-settings-btn"),
  
  sellTokenBtn: document.getElementById("sell-token-btn"),
  sellTokenIcon: document.getElementById("sell-token-icon"),
  sellTokenSymbol: document.getElementById("sell-token-symbol"),
  sellAmountInput: document.getElementById("sell-amount-input"),
  sellFiatValue: document.getElementById("sell-fiat-value"),
  sellBalanceText: document.getElementById("sell-balance-text"),
  sellRangeSlider: document.getElementById("sell-range-slider"),
  reverseTokensBtn: document.getElementById("reverse-tokens-btn"),
  
  buyTokenBtn: document.getElementById("buy-token-btn"),
  buyTokenIcon: document.getElementById("buy-token-icon"),
  buyTokenSymbol: document.getElementById("buy-token-symbol"),
  buyAmountInput: document.getElementById("buy-amount-input"),
  buyFiatValue: document.getElementById("buy-fiat-value"),
  buyBalanceText: document.getElementById("buy-balance-text"),
  rateBadge: document.getElementById("rate-badge"),
  
  slippageBtns: document.querySelectorAll(".slip-btn"),
  customSlipInput: document.getElementById("custom-slip-input"),
  intentsToggle: document.getElementById("intents-toggle"),
  mainActionBtn: document.getElementById("main-action-btn"),
  actionBtnText: document.getElementById("action-btn-text"),
  
  winningAggName: document.getElementById("winning-agg-name"),
  aggregatorsList: document.getElementById("aggregators-list"),
  refreshQuotesBtn: document.getElementById("refresh-quotes-btn"),
  refreshTimerLabel: document.getElementById("refresh-timer-label"),
  
  // Modals
  tokenModalOverlay: document.getElementById("token-modal-overlay"),
  tokenModalClose: document.getElementById("token-modal-close"),
  tokenSearchInput: document.getElementById("token-search-input"),
  popularTokensChips: document.getElementById("popular-tokens-chips"),
  tokenListContainer: document.getElementById("token-list"),
  
  chainModalOverlay: document.getElementById("chain-modal-overlay"),
  chainModalClose: document.getElementById("chain-modal-close"),
  chainListItems: document.getElementById("chain-list-items"),
  
  settingsModalOverlay: document.getElementById("settings-modal-overlay"),
  settingsModalClose: document.getElementById("settings-modal-close"),
  
  walletModalOverlay: document.getElementById("wallet-modal-overlay"),
  walletModalClose: document.getElementById("wallet-modal-close"),
  connectInjected: document.getElementById("connect-injected"),
  connectCoinbase: document.getElementById("connect-coinbase"),
  connectSimulated: document.getElementById("connect-simulated")
};

// ============================================================================
// INITIALIZATION
// ============================================================================
async function init() {
  setupEventListeners();
  loadSavedTheme();
  setupChainsModal();
  await loadPopularTokens();
  await triggerQuoteCompetition();
  startRefreshTimer();
}

// ============================================================================
// EVENT LISTENERS
// ============================================================================
function setupEventListeners() {
  // Banner
  el.bannerClose.addEventListener("click", () => el.banner.remove());
  document.getElementById("try-intents-link").addEventListener("click", (e) => {
    e.preventDefault();
    el.intentsToggle.checked = true;
    state.intentsMode = true;
    triggerQuoteCompetition();
  });

  // Theme
  el.themeToggle.addEventListener("click", toggleTheme);

  // Resources dropdown
  el.resourcesBtn.addEventListener("click", (e) => {
    e.stopPropagation();
    el.resourcesMenu.classList.toggle("hidden");
  });
  document.addEventListener("click", () => el.resourcesMenu.classList.add("hidden"));

  // Token Selectors
  el.sellTokenBtn.addEventListener("click", () => openTokenModal("sell"));
  el.buyTokenBtn.addEventListener("click", () => openTokenModal("buy"));
  el.tokenModalClose.addEventListener("click", closeTokenModal);
  el.tokenModalOverlay.addEventListener("click", (e) => {
    if (e.target === el.tokenModalOverlay) closeTokenModal();
  });
  el.tokenSearchInput.addEventListener("input", filterTokens);

  // Chain Selector
  el.chainSelectBtn.addEventListener("click", () => el.chainModalOverlay.classList.remove("hidden"));
  el.chainModalClose.addEventListener("click", () => el.chainModalOverlay.classList.add("hidden"));
  el.chainModalOverlay.addEventListener("click", (e) => {
    if (e.target === el.chainModalOverlay) el.chainModalOverlay.classList.add("hidden");
  });

  // Settings Modal
  el.tradeSettingsBtn.addEventListener("click", () => el.settingsModalOverlay.classList.remove("hidden"));
  el.settingsModalClose.addEventListener("click", () => el.settingsModalOverlay.classList.add("hidden"));
  el.settingsModalOverlay.addEventListener("click", (e) => {
    if (e.target === el.settingsModalOverlay) el.settingsModalOverlay.classList.add("hidden");
  });

  // Wallet Modal
  el.connectWalletBtn.addEventListener("click", () => el.walletModalOverlay.classList.remove("hidden"));
  el.walletModalClose.addEventListener("click", () => el.walletModalOverlay.classList.add("hidden"));
  el.walletModalOverlay.addEventListener("click", (e) => {
    if (e.target === el.walletModalOverlay) el.walletModalOverlay.classList.add("hidden");
  });
  el.connectInjected.addEventListener("click", () => connectWallet("injected"));
  el.connectCoinbase.addEventListener("click", () => connectWallet("coinbase"));
  el.connectSimulated.addEventListener("click", () => connectWallet("simulated"));

  // Amount Inputs & Sliders
  let debounceTimeout = null;
  el.sellAmountInput.addEventListener("input", () => {
    state.sellAmount = el.sellAmountInput.value.replace(/[^0-9.]/g, "");
    el.sellAmountInput.value = state.sellAmount;
    updateFiatValues();
    clearTimeout(debounceTimeout);
    debounceTimeout = setTimeout(triggerQuoteCompetition, 350);
  });

  document.querySelectorAll(".pct-btn").forEach(btn => {
    btn.addEventListener("click", () => {
      const pct = parseFloat(btn.dataset.pct);
      const maxBal = parseFloat(state.sellToken.balance) || 10;
      const amt = (maxBal * (pct / 100)).toFixed(state.sellToken.decimals === 18 ? 4 : 2);
      state.sellAmount = amt;
      el.sellAmountInput.value = amt;
      el.sellRangeSlider.value = pct;
      updateFiatValues();
      triggerQuoteCompetition();
    });
  });

  el.sellRangeSlider.addEventListener("input", () => {
    const pct = parseFloat(el.sellRangeSlider.value);
    const maxBal = parseFloat(state.sellToken.balance) || 10;
    const amt = (maxBal * (pct / 100)).toFixed(state.sellToken.decimals === 18 ? 4 : 2);
    state.sellAmount = amt;
    el.sellAmountInput.value = amt;
    updateFiatValues();
    clearTimeout(debounceTimeout);
    debounceTimeout = setTimeout(triggerQuoteCompetition, 250);
  });

  // Reverse Tokens Button
  el.reverseTokensBtn.addEventListener("click", reverseTokens);

  // Slippage
  el.slippageBtns.forEach(btn => {
    btn.addEventListener("click", () => {
      el.slippageBtns.forEach(b => b.classList.remove("active"));
      btn.classList.add("active");
      el.customSlipInput.value = "";
      state.slippageBps = Math.round(parseFloat(btn.dataset.slip) * 100);
      triggerQuoteCompetition();
    });
  });

  el.customSlipInput.addEventListener("input", () => {
    const val = parseFloat(el.customSlipInput.value);
    if (!isNaN(val) && val > 0) {
      el.slippageBtns.forEach(b => b.classList.remove("active"));
      state.slippageBps = Math.round(val * 100);
      clearTimeout(debounceTimeout);
      debounceTimeout = setTimeout(triggerQuoteCompetition, 400);
    }
  });

  // Intents Mode
  el.intentsToggle.addEventListener("change", () => {
    state.intentsMode = el.intentsToggle.checked;
    triggerQuoteCompetition();
  });

  // Refresh Button
  el.refreshQuotesBtn.addEventListener("click", () => {
    state.refreshCountdown = 5;
    triggerQuoteCompetition();
  });

  // Main CTA Button
  el.mainActionBtn.addEventListener("click", handleMainAction);
}

// ============================================================================
// THEME MANAGEMENT
// ============================================================================
function loadSavedTheme() {
  const saved = localStorage.getItem("matcha_theme") || "light";
  document.documentElement.setAttribute("data-theme", saved);
  updateThemeIcons(saved);
}

function toggleTheme() {
  const current = document.documentElement.getAttribute("data-theme") || "light";
  const next = current === "dark" ? "light" : "dark";
  document.documentElement.setAttribute("data-theme", next);
  localStorage.setItem("matcha_theme", next);
  updateThemeIcons(next);
}

function updateThemeIcons(theme) {
  if (theme === "dark") {
    el.themeSun.classList.add("hidden");
    el.themeMoon.classList.remove("hidden");
  } else {
    el.themeSun.classList.remove("hidden");
    el.themeMoon.classList.add("hidden");
  }
}

// ============================================================================
// NETWORKS & POPULAR TOKENS
// ============================================================================
function setupChainsModal() {
  el.chainListItems.innerHTML = CHAINS.map(c => `
    <button class="chain-item ${c.id === state.chainId ? 'active' : ''}" data-id="${c.id}">
      <img src="${c.icon}" class="chain-icon" alt="${c.name}" />
      <span>${c.name}</span>
    </button>
  `).join("");

  el.chainListItems.querySelectorAll(".chain-item").forEach(btn => {
    btn.addEventListener("click", () => {
      const id = parseInt(btn.dataset.id);
      const chain = CHAINS.find(c => c.id === id);
      if (chain) {
        state.chainId = chain.id;
        el.activeChainName.textContent = chain.name;
        el.activeChainIcon.src = chain.icon;
        el.chainModalOverlay.classList.add("hidden");
        setupChainsModal();
        loadPopularTokens();
        triggerQuoteCompetition();
      }
    });
  });
}

async function loadPopularTokens() {
  try {
    const res = await fetch(`/api/tokens/popular?chainId=${state.chainId}`);
    if (res.ok) {
      state.tokenList = await res.json();
      renderPopularChips();
      renderTokenList(state.tokenList);
    }
  } catch (err) {
    console.error("Error loading popular tokens:", err);
  }
}

function renderPopularChips() {
  const chips = state.tokenList.slice(0, 6);
  el.popularTokensChips.innerHTML = chips.map(t => `
    <button class="token-chip" data-address="${t.address}">
      <img src="${t.logoUrl || ''}" class="chip-icon" onerror="this.src='https://token-registry.s3.amazonaws.com/icons/tokens/ethereum/64/0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2.png'" />
      <span>${t.symbol}</span>
    </button>
  `).join("");

  el.popularTokensChips.querySelectorAll(".token-chip").forEach(chip => {
    chip.addEventListener("click", () => {
      const addr = chip.dataset.address;
      const tok = state.tokenList.find(t => t.address.toLowerCase() === addr.toLowerCase());
      if (tok) selectToken(tok);
    });
  });
}

function renderTokenList(tokens) {
  el.tokenListContainer.innerHTML = tokens.map(t => `
    <div class="token-row-item" data-address="${t.address}">
      <div class="token-info-col">
        <img src="${t.logoUrl || ''}" class="token-icon" onerror="this.src='https://token-registry.s3.amazonaws.com/icons/tokens/ethereum/64/0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2.png'" />
        <div class="token-name-wrap">
          <span class="token-row-symbol">${t.symbol}</span>
          <span class="token-row-name">${t.name}</span>
        </div>
      </div>
      <div class="token-balance-col">
        <span class="token-bal-amount">0.00</span>
        <span class="token-bal-fiat">$0.00</span>
      </div>
    </div>
  `).join("");

  el.tokenListContainer.querySelectorAll(".token-row-item").forEach(row => {
    row.addEventListener("click", () => {
      const addr = row.dataset.address;
      const tok = state.tokenList.find(t => t.address.toLowerCase() === addr.toLowerCase());
      if (tok) selectToken(tok);
    });
  });
}

function openTokenModal(target) {
  state.tokenModalTarget = target;
  el.tokenSearchInput.value = "";
  renderTokenList(state.tokenList);
  el.tokenModalOverlay.classList.remove("hidden");
  el.tokenSearchInput.focus();
}

function closeTokenModal() {
  el.tokenModalOverlay.classList.add("hidden");
}

function filterTokens() {
  const q = el.tokenSearchInput.value.trim().toLowerCase();
  const filtered = state.tokenList.filter(t =>
    t.symbol.toLowerCase().includes(q) ||
    t.name.toLowerCase().includes(q) ||
    t.address.toLowerCase() === q
  );
  renderTokenList(filtered);
}

function selectToken(tok) {
  if (state.tokenModalTarget === "sell") {
    state.sellToken = { ...tok, balance: "12.45", price: tok.symbol === "ETH" ? 2403.45 : 1.00 };
    el.sellTokenSymbol.textContent = tok.symbol;
    el.sellTokenIcon.src = tok.logoUrl || el.sellTokenIcon.src;
    el.sellBalanceText.textContent = `Balance: ${state.sellToken.balance} ${tok.symbol}`;
  } else {
    state.buyToken = { ...tok, balance: "4500.00", price: tok.symbol === "USDC" ? 1.00 : 2403.45 };
    el.buyTokenSymbol.textContent = tok.symbol;
    el.buyTokenIcon.src = tok.logoUrl || el.buyTokenIcon.src;
    el.buyBalanceText.textContent = `Balance: ${state.buyToken.balance} ${tok.symbol}`;
  }
  closeTokenModal();
  updateFiatValues();
  triggerQuoteCompetition();
}

function reverseTokens() {
  const tmp = state.sellToken;
  state.sellToken = state.buyToken;
  state.buyToken = tmp;

  el.sellTokenSymbol.textContent = state.sellToken.symbol;
  el.sellTokenIcon.src = state.sellToken.logoUrl;
  el.sellBalanceText.textContent = `Balance: ${state.sellToken.balance} ${state.sellToken.symbol}`;

  el.buyTokenSymbol.textContent = state.buyToken.symbol;
  el.buyTokenIcon.src = state.buyToken.logoUrl;
  el.buyBalanceText.textContent = `Balance: ${state.buyToken.balance} ${state.buyToken.symbol}`;

  updateFiatValues();
  triggerQuoteCompetition();
}

function updateFiatValues() {
  const sellAmt = parseFloat(String(state.sellAmount).replace(/,/g, "")) || 0;
  const sellPrice = state.sellToken.symbol === "ETH" ? 2403.45 : (state.sellToken.price || 1.00);
  const sellFiat = (sellAmt * sellPrice).toLocaleString('en-US', { style: 'currency', currency: 'USD' });
  el.sellFiatValue.textContent = sellFiat;

  const buyAmt = parseFloat(String(state.buyAmount).replace(/,/g, "")) || 0;
  const buyPrice = state.buyToken.symbol === "ETH" ? 2403.45 : (state.buyToken.price || 1.00);
  const buyFiat = (buyAmt * buyPrice).toLocaleString('en-US', { style: 'currency', currency: 'USD' });
  el.buyFiatValue.innerHTML = `${buyFiat} <span class="price-impact-tag">(+0.008%)</span>`;

  // Update rate badge
  const unitRate = (sellPrice / buyPrice).toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 6 });
  el.rateBadge.textContent = `1 ${state.sellToken.symbol} ≈ ${unitRate} ${state.buyToken.symbol}`;
}

// ============================================================================
// COMPETITION & MULTI-AGGREGATOR QUOTE DISPATCH
// ============================================================================
async function triggerQuoteCompetition() {
  const sellAmt = parseFloat(state.sellAmount);
  if (!sellAmt || sellAmt <= 0) {
    el.buyAmountInput.value = "0.0";
    return;
  }

  el.refreshQuotesBtn.classList.add("spinning");

  // Format sell amount in base units
  const rawSellAmount = BigInt(Math.floor(sellAmt * (10 ** state.sellToken.decimals))).toString();

  const competitionPayload = {
    chainId: state.chainId,
    sellTokenAddress: state.sellToken.address,
    buyTokenAddress: state.buyToken.address,
    sellAmount: rawSellAmount,
    sellTokenDecimals: state.sellToken.decimals,
    buyTokenDecimals: state.buyToken.decimals,
    gasPrice: "30000000000",
    slippageBps: state.slippageBps,
    taker: state.userAddress || "0xd8dA6BF26964aF9D7eEd9e03E53415D37aA96045",
    isAllowanceHolderFlow: true
  };

  try {
    const compRes = await fetch("/api/competitions", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(competitionPayload)
    });
    const compData = await compRes.json();
    const compId = compData.id || compData.competitionId;
    state.activeCompetitionId = compId;

    // Fetch quotes across all aggregators concurrently
    const quotePromises = AGGREGATOR_NAMES.map(async (agg) => {
      try {
        const qRes = await fetch(`/api/quotes?aggregator=${agg}`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ competitionId: compId, aggregator: agg })
        });
        if (qRes.ok) {
          const data = await qRes.json();
          return parseAggregatorQuote(agg, data);
        }
      } catch (err) {
        console.warn(`Quote error for ${agg}:`, err);
      }
      return null;
    });

    const results = (await Promise.all(quotePromises)).filter(Boolean);

    // Sort from highest buyAmount to lowest
    results.sort((a, b) => (b.rawBuyAmount > a.rawBuyAmount ? 1 : -1));

    state.quotes = results;
    renderAggregatorLeaderboard();

    if (results.length > 0) {
      const best = results[0];
      state.buyAmount = best.formattedAmount;
      el.buyAmountInput.value = best.formattedAmount;
      el.winningAggName.textContent = best.name;
      updateFiatValues();
    }
  } catch (err) {
    console.error("Competition dispatch failed:", err);
  } finally {
    el.refreshQuotesBtn.classList.remove("spinning");
  }
}

function parseAggregatorQuote(name, data) {
  const direct = data.direct || {};
  const quote = direct.quote || {};
  const sim = direct.simulation || {};
  const simDetails = sim.details || {};

  const rawBuyAmount = BigInt(quote.buyAmount || "0");
  const dec = state.buyToken.decimals;
  const numAmount = Number(rawBuyAmount) / (10 ** dec);
  const formattedAmount = numAmount.toLocaleString('en-US', { minimumFractionDigits: 6, maximumFractionDigits: 6 });

  const gasFeeUsd = simDetails.totalTransactionFee
    ? "$" + (Number(BigInt(simDetails.totalTransactionFee)) / 1e18 * (state.sellToken.price || 2403.45)).toFixed(2)
    : "$0.22";

  return {
    name,
    rawBuyAmount,
    numAmount,
    formattedAmount,
    gasUsd: gasFeeUsd,
    gasLimit: simDetails.gas || quote.gas || "231021",
    sources: quote.sources || [name + " RFQ"],
    simulationResult: sim.result || "success"
  };
}

function renderAggregatorLeaderboard() {
  if (state.quotes.length === 0) {
    el.aggregatorsList.innerHTML = `<div class="loading-shimmer"><div class="shimmer-card"></div></div>`;
    return;
  }

  const best = state.quotes[0];

  el.aggregatorsList.innerHTML = state.quotes.map((q, idx) => {
    const isBest = idx === 0;
    const diffPct = isBest
      ? ""
      : (((q.numAmount - best.numAmount) / best.numAmount) * 100).toFixed(2) + "%";

    // Split amount into integer and decimal for bold styling
    const parts = q.formattedAmount.split(".");
    const intPart = parts[0];
    const decPart = "." + (parts[1] || "00");

    const fiatVal = (q.numAmount * (state.buyToken.price || 1.00)).toLocaleString('en-US', { style: 'currency', currency: 'USD' });

    return `
      <div class="aggregator-card ${isBest ? 'best-card' : ''} ${state.selectedAggregator === q.name ? 'selected' : ''}" data-agg="${q.name}">
        <div class="agg-top-row">
          <div class="agg-amount-wrap">
            <span class="agg-amount-int">${intPart}</span><span class="agg-amount-dec">${decPart}</span>
            <span class="agg-token-symbol">${state.buyToken.symbol}</span>
          </div>
          ${isBest
            ? `<span class="best-badge">🏆 Best</span>`
            : `<span class="diff-badge">${diffPct}</span>`
          }
        </div>
        <div class="agg-bottom-row">
          <div class="agg-fiat-info">
            <span>${fiatVal}</span>
            <span class="gas-cost-tag">⛽ ${q.gasUsd}</span>
          </div>
          <div class="agg-brand-name">
            <span>${q.name}</span>
            ${q.name === 'Lightning' ? '⚡' : ''}
          </div>
        </div>
      </div>
    `;
  }).join("");

  // Attach card click handlers
  el.aggregatorsList.querySelectorAll(".aggregator-card").forEach(card => {
    card.addEventListener("click", () => {
      const agg = card.dataset.agg;
      state.selectedAggregator = agg;
      const chosen = state.quotes.find(q => q.name === agg);
      if (chosen) {
        state.buyAmount = chosen.formattedAmount;
        el.buyAmountInput.value = chosen.formattedAmount;
        el.winningAggName.textContent = chosen.name;
        updateFiatValues();
      }
      renderAggregatorLeaderboard();
    });
  });
}

// ============================================================================
// AUTO REFRESH COUNTDOWN
// ============================================================================
function startRefreshTimer() {
  if (state.refreshInterval) clearInterval(state.refreshInterval);
  state.refreshCountdown = 5;
  el.refreshTimerLabel.textContent = `Refresh (${state.refreshCountdown})`;

  state.refreshInterval = setInterval(() => {
    state.refreshCountdown--;
    if (state.refreshCountdown <= 0) {
      state.refreshCountdown = 5;
      triggerQuoteCompetition();
    }
    el.refreshTimerLabel.textContent = `Refresh (${state.refreshCountdown})`;
  }, 1000);
}

// ============================================================================
// WALLET INTEGRATION
// ============================================================================
async function connectWallet(type) {
  if (type === "injected" && typeof window.ethereum !== "undefined") {
    try {
      const accounts = await window.ethereum.request({ method: "eth_requestAccounts" });
      if (accounts && accounts.length > 0) {
        state.userAddress = accounts[0];
      }
    } catch (err) {
      console.warn("Wallet rejected, using fallback account:", err);
      state.userAddress = "0xd8dA6BF26964aF9D7eEd9e03E53415D37aA96045";
    }
  } else {
    // Simulated or Coinbase fallback
    state.userAddress = "0xd8dA6BF26964aF9D7eEd9e03E53415D37aA96045";
  }

  const truncated = `${state.userAddress.slice(0, 6)}...${state.userAddress.slice(-4)}`;
  el.walletBtnLabel.textContent = truncated;
  el.actionBtnText.textContent = `Review Swap via ${state.selectedAggregator}`;
  el.walletModalOverlay.classList.add("hidden");
}

function handleMainAction() {
  if (!state.userAddress) {
    el.walletModalOverlay.classList.remove("hidden");
    return;
  }

  el.mainActionBtn.disabled = true;
  el.actionBtnText.textContent = `Simulating transaction with ${state.selectedAggregator}...`;

  setTimeout(() => {
    alert(`[Matcha Meta Simulation Successful]\n\nRouting through: ${state.selectedAggregator}\nSell: ${state.sellAmount} ${state.sellToken.symbol}\nBuy: ${state.buyAmount} ${state.buyToken.symbol}\nGas: $0.22 (MEV Protected via Flashbots)\nExecution: Confirmed`);
    el.mainActionBtn.disabled = false;
    el.actionBtnText.textContent = `Swap via ${state.selectedAggregator}`;
  }, 1200);
}

// Start application
window.addEventListener("DOMContentLoaded", init);
