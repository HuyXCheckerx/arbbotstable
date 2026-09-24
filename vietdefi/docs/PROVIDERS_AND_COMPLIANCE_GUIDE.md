# VietDeFi (VND-X): Infrastructure Providers & Regulatory Compliance Guide

A complete operational guide for the third-party providers, hosting infrastructure, and regulatory sandboxes powering VietDeFi.

---

## 1. Cloudflare (Pages, Workers & Edge Security)
- **Dashboard URL**: [https://dash.cloudflare.com/sign-up/workers-and-pages](https://dash.cloudflare.com/sign-up/workers-and-pages)
- **Role in VietDeFi**:
  - **Pages**: Global CDN hosting for the Vite/Next.js frontend. Edge distribution ensures < 25ms TTFB across Vietnam via VNPT, Viettel, and FPT telecom peering points in Ho Chi Minh City and Da Nang.
  - **Workers**: Serverless Edge API Gateway running request routing, IP rate limiting, and Cap CAPTCHA challenge verification.
  - **WAF & Security**: Layer 7 DDoS mitigation, TLS 1.3 strict encryption, automatic SSL/TLS certificate rotation, and HSTS.

### Recommended Cloudflare Worker Configuration (`wrangler.toml`):
```toml
name = "vietdefi-edge-api"
main = "src/worker.ts"
compatibility_date = "2026-09-01"

[vars]
ENVIRONMENT = "production"
CORS_ORIGIN = "https://vietdefi.vn"

# Secrets (configured via wrangler secret put)
# - CAP_SECRET
# - SUPABASE_SERVICE_ROLE_KEY
```

---

## 2. Supabase (Managed Postgres, Auth, Realtime & Storage)
- **URL**: [https://supabase.com/](https://supabase.com/)
- **Role in VietDeFi**:
  - **PostgreSQL 16 Engine**: Multi-tenant database holding vault positions, lending escrow records, tax transactions, and audit trails.
  - **Row Level Security (RLS)**: Enforces cryptographic data isolation at the database layer (`auth.uid() = user_id`).
  - **Web3 SIWE Integration**: Maps EIP-4361 Ethereum wallet signatures to Supabase Auth UUIDs.
  - **Decree 13 Compliant Storage**: Client-side AES-256 encrypted buckets for national ID cards (CCCD) and generated tax reports.

### Quick Setup Steps:
1. Create a new project in Supabase (Select `Southeast Asia (Singapore)` region for lowest latency to Vietnam).
2. Open the SQL Editor and run the migration in:
   `vietdefi/supabase/migrations/001_initial_schema.sql`
3. Copy `SUPABASE_URL` and `SUPABASE_ANON_KEY` to `.env`.

---

## 3. Vietnamese Regulatory Exchanges & Fiat Rails (CAEX & SCEX)
- **CAEX**: [https://www.caex.com.vn/](https://www.caex.com.vn/)
  - *Entity*: Công ty Cổ phần Sàn giao dịch Tài sản mã hóa Việt Nam Thịnh Vượng.
  - *Context*: Sàn giao dịch đang trong quá trình xin cấp phép theo khuôn khổ thí điểm của **Nghị quyết 05/2025/NQ-CP**, hợp tác công nghệ chiến lược với HashKey, cổ đông chiến lược VPBankS và LynkiD.
- **SCEX**: [https://scex.com.vn/](https://scex.com.vn/)
  - Sàn giao dịch số / hàng hóa Sài Gòn.
- **VietDeFi Sandbox Positioning**:
  - Non-custodial DeFi protocols interact seamlessly with domestic fiat rails.
  - Users can bridge on-chain VNDL to fiat VND via VietQR (NAPAS 24/7) banking APIs (VPBank, Vietcombank, Techcombank) or partner with CAEX licensed liquidity providers.

---

## 4. Cap CAPTCHA (trycap.dev)
- **Documentation**: [https://trycap.dev](https://trycap.dev/)
- **Guide & Specs**: [https://trycap.dev/prompt.md](https://trycap.dev/prompt.md)
- **Why Cap over Google reCAPTCHA / Cloudflare Turnstile**:
  - **Zero User Tracking**: Runs on sovereign infrastructure, phones home to nobody, eliminating third-party data processor agreements under GDPR and Decree 13.
  - **No Puzzles**: Proof-of-work solves in ~1s without frustrating CAPTCHA image puzzles.
  - **Permanent Free & Open Source**: Apache 2.0 license.
- **Implementation in VietDeFi**:
  - Client component uses `<cap-widget>` with `solve` event listeners.
  - Backend verifies challenge token with atomic single-use nonce deletion (`getdel`), completely stopping replay attacks.

---

## 5. Vietnamese Legal Compliance Framework
1. **Resolution 05/2025/NQ-CP (Nghị quyết 05/2025/NQ-CP)**:
   - National pilot program for crypto asset exchange operations and digital asset custody in Vietnam.
2. **Decree 13/2023/NĐ-CP (Nghị định 13/2023/NĐ-CP)**:
   - Personal Data Protection Decree (PDPD). Enforces data encryption, consent records, and right to be forgotten.
3. **Law on Personal Income Tax (Luật Thuế TNCN)**:
   - Automated FIFO cost basis tracking for capital gains calculations with downloadable Form 02/KK-TNCN.
