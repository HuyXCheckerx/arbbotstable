# VietDeFi (VND-X): Platform Walkthrough & Validation

We have created, scaffolded, and validated **VietDeFi (VND-X)**—a Web3 financial protocol and regulatory compliance suite built specifically for the Vietnamese digital asset economy.

---

## 1. Summary of Completed Deliverables

### A. Web3 Application & Core UI (`/vietdefi`)
1. **Interactive Above-the-Fold CTA Widget**:
   - **1:1 Stablecoin Swap (PSM)**: Zero-slippage hoán đổi between **USDT, USDC, PYUSD, and USDG** with 0.02% protocol fee.
   - **VNDL CDP Minting**: Over-collateralized minting of VNDL (1 VNDL = 1 VND) backed by USDT (120% MCR) and Tether Gold XAUt (140% MCR), live health factor simulator, and Dutch auction liquidation triggers.
   - **Dual-Track Lending Market**: Supply crypto to earn up to **9.5% APY** paid in VND / VNDL; borrow VND cash against BTC/ETH without selling assets.
2. **Vietnam Crypto Tax Service (PIT / Thuế TNCN)**:
   - Built to conform to **Resolution 05/2025/NQ-CP** and **Decree 13/2023/NĐ-CP** (PDPD).
   - Dynamic toggling between **FIFO** and **Weighted Average (Bình quân gia quyền)** cost-basis methods.
   - Calculation of Gross Turnover, Cost Basis, Net Taxable Gains, and Estimated 5% PIT.
   - Export preview for standard Vietnamese Tax Form **Mẫu số 02/KK-TNCN (Tài Sản Số)**.
3. **Cap Self-Hosted CAPTCHA Integration (`trycap.dev`)**:
   - Implemented per `https://trycap.dev/prompt.md` using client-safe proof-of-work challenge generation and server-side fail-closed token verification with single-use replay protection.
4. **Mandatory Pages & SEO Assets**:
   - **Custom 404 Page** (`/404`) with quick links to Swap, Lending, Tax, and search bar.
   - **Thank You / Transaction Success Page** (`/thank-you`) with celebratory confetti, block confirmations, transaction hash, and downloadable receipt.
   - **Dynamic Sitemap** (`public/sitemap.xml`) with priority and change frequencies.
   - **Robots Configuration** (`public/robots.txt`) allowing crawler indexing while protecting private endpoints.
   - **Rich SEO & Social Sharing**: Complete Schema.org JSON-LD structured data (`Organization`, `FinancialProduct`, `SoftwareApplication`), Open Graph, and Twitter Cards.
   - **Internal Linking Network**: Comprehensive 4-column footer connecting products, legal sandboxes (CAEX/SCEX), and infrastructure providers.

---

### B. Production Smart Contracts (`/vietdefi/contracts`)
- [VNDL.sol](file:///Users/perycent/Downloads/arbbot/vietdefi/contracts/VNDL.sol): Decentralized Vietnamese Dong stablecoin ERC-20 with role-based minting and burning.
- [VNDLVault.sol](file:///Users/perycent/Downloads/arbbot/vietdefi/contracts/VNDLVault.sol): Multi-collateral CDP Vault supporting USDT & XAUt with Chainlink/Pyth oracle valuation and liquidation engine.
- [StablePSM.sol](file:///Users/perycent/Downloads/arbbot/vietdefi/contracts/StablePSM.sol): Peg Stability Module executing 1:1 stablecoin swaps with circuit breakers.
- [CryptoVNDLendingPool.sol](file:///Users/perycent/Downloads/arbbot/vietdefi/contracts/CryptoVNDLendingPool.sol): Dual-track crypto lending pool paying VNDL yield.

---

### C. Backend & Security Suite (`/vietdefi/supabase`)
- [001_initial_schema.sql](file:///Users/perycent/Downloads/arbbot/vietdefi/supabase/migrations/001_initial_schema.sql): PostgreSQL schema with:
  - Strict Row-Level Security (RLS) policies for complete multi-tenant isolation.
  - Decree 13/2023/NĐ-CP compliant encrypted PII storage.
  - Tamper-evident immutable audit logs with SHA-256 hash chaining.
- [capService.ts](file:///Users/perycent/Downloads/arbbot/vietdefi/src/services/capService.ts): Cap challenge generation, redeem validation, and fail-closed token verification.
- [taxService.ts](file:///Users/perycent/Downloads/arbbot/vietdefi/src/services/taxService.ts): Vietnam crypto tax engine with FIFO and weighted-average algorithms.

---

## 2. Visual Walkthrough & Validation Results

### Interactive Hero Section & CTA Above the Fold
The landing page greets users with a dark-mode glassmorphic interface, real-time protocol stats ($148.5M volume, 0.02% fee, 10.5% lending APR), and an interactive quick widget directly in the viewport.

![Landing Page Hero](/Users/perycent/.gemini/antigravity-ide/brain/1de792e8-943b-457c-9468-c4ce1921cd81/landing_page_hero_1790217817239.png)

---

### VNDL CDP Vault & Crypto Lending Tabs
Users can switch between swapping dollar stablecoins, minting gold/dollar-backed VNDL, and calculating monthly VND yields on crypto savings.

````carousel
![VNDL CDP Minting Tab](/Users/perycent/.gemini/antigravity-ide/brain/1de792e8-943b-457c-9468-c4ce1921cd81/duc_vndl_tab_1790217831055.png)
<!-- slide -->
![Lending Yield Tab](/Users/perycent/.gemini/antigravity-ide/brain/1de792e8-943b-457c-9468-c4ce1921cd81/gui_lai_vnd_tab_1790217846071.png)
````

---

### Cap CAPTCHA Challenge & Verification Modal
When initiating a transaction, the user must solve the Cap self-hosted proof-of-work challenge before the order is signed. Once verified, the button turns green with a checkmark, and the server-side fail-closed check executes.

````carousel
![Unverified Cap Challenge](/Users/perycent/.gemini/antigravity-ide/brain/1de792e8-943b-457c-9468-c4ce1921cd81/tx_modal_unverified_1790217869085.png)
<!-- slide -->
![Verified Cap Proof-of-Work](/Users/perycent/.gemini/antigravity-ide/brain/1de792e8-943b-457c-9468-c4ce1921cd81/tx_modal_verified_1790217880756.png)
````

---

### Transaction Confirmation & Thank You Page
Upon confirmation, users are taken to the celebration page featuring live confetti animations, block confirmation counts, on-chain TxHash, and an option to download official VietQR transaction receipts.

![Transaction Success Page](/Users/perycent/.gemini/antigravity-ide/brain/1de792e8-943b-457c-9468-c4ce1921cd81/tx_success_page_1790217894922.png)

---

### Vietnam Crypto Tax Calculation Engine
Comprehensive overview cards detailing Total Turnover, Cost Basis, Net Taxable Gains, and Estimated 5% PIT under Resolution 05/2025/NQ-CP sandbox guidelines. Users can switch between FIFO and Weighted Average cost-basis methods and view itemized ledger entries.

![Tax Calculator Page](/Users/perycent/.gemini/antigravity-ide/brain/1de792e8-943b-457c-9468-c4ce1921cd81/tax_calculator_page_1790217952028.png)

---

### Custom 404 Error Page
Polished 404 error screen featuring cyberpunk gradients, quick navigation buttons to core functions, documentation search, and system footer.

![Custom 404 Page](/Users/perycent/.gemini/antigravity-ide/brain/1de792e8-943b-457c-9468-c4ce1921cd81/custom_404_page_1790217970456.png)

---

### Browser Session Recording
The full interactive browser session demonstrating all user flows was recorded and saved:

![VietDeFi Flow Recording](/Users/perycent/.gemini/antigravity-ide/brain/1de792e8-943b-457c-9468-c4ce1921cd81/vietdefi_flow_demo_1790217798010.webp)

---

## 3. How to Run Locally

```bash
# Navigate to the project directory
cd /Users/perycent/Downloads/arbbot/vietdefi

# Run the development server
npm run dev

# Or build and run production preview
npm run build
npm run preview
```
Visit `http://localhost:5173/` in your browser.
