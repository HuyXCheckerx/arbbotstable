-- VietDeFi / VND-X Supabase PostgreSQL Schema
-- Compliant with Vietnam Decree 13/2023/ND-CP and Resolution 05/2025/NQ-CP

-- Enable required extensions
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";
CREATE EXTENSION IF NOT EXISTS "pgcrypto";

-- =============================================================================
-- 1. USERS & PROFILES TABLE
-- =============================================================================
CREATE TYPE user_kyc_status AS ENUM ('unverified', 'tier1_wallet_verified', 'tier2_cccd_verified', 'rejected');
CREATE TYPE user_role AS ENUM ('user', 'liquidity_provider', 'auditor', 'admin');

CREATE TABLE IF NOT EXISTS public.profiles (
    id UUID PRIMARY KEY REFERENCES auth.users(id) ON DELETE CASCADE,
    wallet_address TEXT UNIQUE,
    full_name TEXT,
    cccd_number_hash TEXT UNIQUE, -- SHA-256 encrypted for Decree 13 compliance
    phone_number TEXT,
    bank_account_number TEXT,
    bank_name TEXT,
    kyc_status user_kyc_status DEFAULT 'unverified',
    role user_role DEFAULT 'user',
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

-- =============================================================================
-- 2. VNDL CDP VAULT POSITIONS
-- =============================================================================
CREATE TABLE IF NOT EXISTS public.vault_positions (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    user_id UUID NOT NULL REFERENCES public.profiles(id) ON DELETE CASCADE,
    wallet_address TEXT NOT NULL,
    collateral_asset TEXT NOT NULL, -- 'USDT' or 'XAUt'
    collateral_amount NUMERIC(36, 18) NOT NULL DEFAULT 0,
    minted_vndl NUMERIC(36, 18) NOT NULL DEFAULT 0,
    health_factor NUMERIC(8, 4) DEFAULT 999.99,
    is_liquidated BOOLEAN DEFAULT FALSE,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

-- =============================================================================
-- 3. CRYPTO & VND LENDING MARKET
-- =============================================================================
CREATE TABLE IF NOT EXISTS public.lending_positions (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    user_id UUID NOT NULL REFERENCES public.profiles(id) ON DELETE CASCADE,
    wallet_address TEXT NOT NULL,
    pool_type TEXT NOT NULL, -- 'supply_crypto_earn_vnd' or 'borrow_vnd_lock_crypto'
    supplied_asset TEXT,     -- 'USDT', 'BTC', 'ETH'
    supplied_amount NUMERIC(36, 18) DEFAULT 0,
    borrowed_vnd NUMERIC(36, 2) DEFAULT 0,
    earned_vnd_yield NUMERIC(36, 2) DEFAULT 0,
    status TEXT DEFAULT 'active',
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

-- =============================================================================
-- 4. VIETNAMESE CRYPTO TAX TRANSACTIONS (FIFO / TNCN)
-- =============================================================================
CREATE TYPE tax_calculation_method AS ENUM ('fifo', 'specific_id', 'weighted_average');

CREATE TABLE IF NOT EXISTS public.tax_transactions (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    user_id UUID NOT NULL REFERENCES public.profiles(id) ON DELETE CASCADE,
    tx_hash TEXT NOT NULL,
    chain_id INT DEFAULT 8453, -- Base or Arbitrum
    timestamp TIMESTAMPTZ NOT NULL,
    tx_type TEXT NOT NULL,     -- 'swap', 'lending_yield', 'borrow_liquidation', 'fiat_exit'
    asset_sold TEXT,
    amount_sold NUMERIC(36, 18) DEFAULT 0,
    cost_basis_vnd NUMERIC(36, 2) DEFAULT 0,
    asset_bought TEXT,
    amount_bought NUMERIC(36, 18) DEFAULT 0,
    proceeds_vnd NUMERIC(36, 2) DEFAULT 0,
    net_gain_loss_vnd NUMERIC(36, 2) DEFAULT 0,
    calculated_tax_vnd NUMERIC(36, 2) DEFAULT 0,
    calculation_method tax_calculation_method DEFAULT 'fifo',
    created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS public.tax_reports (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    user_id UUID NOT NULL REFERENCES public.profiles(id) ON DELETE CASCADE,
    tax_year INT NOT NULL,
    filing_status TEXT DEFAULT 'draft',
    total_taxable_gains_vnd NUMERIC(36, 2) NOT NULL DEFAULT 0,
    estimated_pit_payable_vnd NUMERIC(36, 2) NOT NULL DEFAULT 0,
    report_pdf_url TEXT,
    submitted_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

-- =============================================================================
-- 5. TAMPER-EVIDENT HASH-CHAINED AUDIT LOGS
-- =============================================================================
CREATE TABLE IF NOT EXISTS public.tamper_evident_audit_logs (
    id BIGSERIAL PRIMARY KEY,
    previous_hash TEXT NOT NULL,
    current_hash TEXT NOT NULL,
    actor_id UUID REFERENCES public.profiles(id),
    action TEXT NOT NULL,
    entity_name TEXT NOT NULL,
    entity_id TEXT NOT NULL,
    ip_address TEXT,
    payload JSONB NOT NULL,
    recorded_at TIMESTAMPTZ DEFAULT NOW()
);

-- =============================================================================
-- 6. ROW LEVEL SECURITY (RLS) POLICIES
-- =============================================================================
ALTER TABLE public.profiles ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.vault_positions ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.lending_positions ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.tax_transactions ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.tax_reports ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.tamper_evident_audit_logs ENABLE ROW LEVEL SECURITY;

-- Profiles: Users can view and update only their own profile
CREATE POLICY "Users can view own profile" 
    ON public.profiles FOR SELECT 
    USING (auth.uid() = id);

CREATE POLICY "Users can update own profile" 
    ON public.profiles FOR UPDATE 
    USING (auth.uid() = id);

-- Vault positions: Users can view and manage only their own positions
CREATE POLICY "Users can view own vault positions" 
    ON public.vault_positions FOR SELECT 
    USING (auth.uid() = user_id);

CREATE POLICY "Users can insert own vault positions" 
    ON public.vault_positions FOR INSERT 
    WITH CHECK (auth.uid() = user_id);

-- Lending positions: User isolation
CREATE POLICY "Users can view own lending positions" 
    ON public.lending_positions FOR SELECT 
    USING (auth.uid() = user_id);

CREATE POLICY "Users can manage own lending positions" 
    ON public.lending_positions FOR ALL 
    USING (auth.uid() = user_id);

-- Tax records: Strict privacy per Decree 13
CREATE POLICY "Users can view own tax transactions" 
    ON public.tax_transactions FOR SELECT 
    USING (auth.uid() = user_id);

CREATE POLICY "Users can insert own tax transactions" 
    ON public.tax_transactions FOR INSERT 
    WITH CHECK (auth.uid() = user_id);

CREATE POLICY "Users can view own tax reports" 
    ON public.tax_reports FOR SELECT 
    USING (auth.uid() = user_id);

-- Audit logs: Read-only for authorized compliance auditors, append-only via server service role
CREATE POLICY "Auditors can view audit logs" 
    ON public.tamper_evident_audit_logs FOR SELECT 
    USING (
        EXISTS (
            SELECT 1 FROM public.profiles 
            WHERE profiles.id = auth.uid() AND profiles.role IN ('admin', 'auditor')
        )
    );
