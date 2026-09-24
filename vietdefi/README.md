# VietDeFi (VND-X) — Vietnam Web3 Finance & Crypto Tax Portal

VietDeFi (VND-X) is an enterprise-grade, regulation-ready decentralized financial ecosystem and Web3 compliance portal engineered specifically for the Vietnamese digital asset economy.

---

## 📁 Repository Directory Structure

```text
vietdefi/
├── contracts/                        # Production Solidity Smart Contracts (EVM)
│   ├── VNDL.sol                      # ERC-20 Vietnamese Dong Stablecoin (1 VNDL = 1 VND)
│   ├── VNDLVault.sol                 # Over-collateralized CDP Vault (USDT & XAUt backing)
│   ├── StablePSM.sol                 # Peg Stability Module: 1:1 Stablecoin Swaps (USDT/USDC/PYUSD/USDG)
│   └── CryptoVNDLendingPool.sol      # Crypto Collateral & VND Yield Lending Pool
├── docs/                             # Full Architectural & Operational Documentation
│   ├── ARCHITECTURE_PLAN.md          # Master architecture blueprint, Mermaid diagrams & ADRs
│   ├── WALKTHROUGH.md                # System walkthrough & UI test validation report
│   └── PROVIDERS_AND_COMPLIANCE_GUIDE.md # Guide for Cloudflare, Supabase, CAEX/SCEX, Cap CAPTCHA & NQ 05
├── public/                           # Static Public Assets & SEO Infrastructure
│   ├── robots.txt                    # Search crawler indexing rules
│   ├── sitemap.xml                   # XML sitemap with route priorities and change frequencies
│   └── vite.svg                      # Platform favicon and branding icons
├── src/                              # Client & Core Frontend Source (React + TypeScript)
│   ├── services/
│   │   ├── capService.ts             # Cap CAPTCHA challenge & replay-protected verification
│   │   └── taxService.ts             # Vietnam Crypto Tax Engine (FIFO/Cost-Basis under NQ 05 & PIT)
│   ├── App.tsx                       # Main application: Swap, Vault, Lending, Tax & 404 views
│   ├── index.css                     # Vanilla CSS design system (Cyberpunk Navy, Glassmorphism)
│   └── main.tsx                      # Application root entry point with Buffer polyfills
├── supabase/
│   └── migrations/
│       └── 001_initial_schema.sql    # PostgreSQL schema with Row-Level Security & Decree 13 PII rules
├── index.html                        # HTML5 entry with complete SEO, OpenGraph, JSON-LD & Cap script
├── package.json                      # Node.js dependencies and operational scripts
├── tsconfig.json                     # TypeScript configuration
└── vite.config.ts                    # Vite build configuration with Node polyfills
```

---

## 🚀 Key Features

1. **1:1 Stablecoin Swap (PSM)**: Zero-slippage hoán đổi between **USDT, USDC, PYUSD, and USDG** with 0.02% protocol fee.
2. **VNDL Stablecoin (Over-Collateralized CDP)**: 1:1 VND peg backed by **Tether USD (120% MCR)** and **Tether Gold XAUt (140% MCR)** with real-time Chainlink/Pyth oracle consensus.
3. **Vietnamese Dual-Track Lending**:
   - Supply crypto to earn up to **9.5% APY** in VND cash.
   - Borrow VND liquidity against BTC/ETH collateral without triggering taxable asset sales.
4. **Vietnam Crypto Tax Engine (PIT / Thuế TNCN)**:
   - Compliant with **Resolution 05/2025/NQ-CP** and **Decree 13/2023/NĐ-CP**.
   - Supports **FIFO** and **Weighted Average** cost basis calculation.
   - Generates official Vietnamese tax report **Mẫu số 02/KK-TNCN (Tài Sản Số)**.
5. **Self-Hosted Cap CAPTCHA (`trycap.dev`)**:
   - Privacy-preserving proof-of-work bot detection.
   - Zero third-party data tracking.
   - Single-use replay protection.
6. **Conversion & SEO Essentials**:
   - CTA Above the Fold with live interactive widget.
   - Custom 404 page (`/404`) and Thank You confirmation page (`/thank-you`).
   - Dynamic `sitemap.xml`, `robots.txt`, and Schema.org structured data.

---

## 🛠️ How to Run Locally

```bash
# 1. Install dependencies
npm install

# 2. Run in development mode
npm run dev

# 3. Build and preview production release
npm run build
npm run preview -- --port 5173
```

Open `http://localhost:5173/` in your browser.
