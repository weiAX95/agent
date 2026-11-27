-- M4 预算只在 M2 Run 迁移后创建；现有聊天和 M3 用量账本不改写。
CREATE TABLE IF NOT EXISTS public.gateway_budget_accounts (
    id uuid PRIMARY KEY,
    owner_id uuid NOT NULL REFERENCES public.users(id) ON DELETE RESTRICT,
    currency varchar(3) NOT NULL,
    budget_date date NOT NULL,
    limit_amount numeric(30,12) NOT NULL,
    spent_amount numeric(30,12) NOT NULL DEFAULT 0,
    reserved_amount numeric(30,12) NOT NULL DEFAULT 0,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT uq_gateway_budget_scope UNIQUE (owner_id, currency, budget_date),
    CONSTRAINT ck_gateway_budget_amounts CHECK (
        limit_amount > 0 AND spent_amount >= 0 AND reserved_amount >= 0
    )
);

-- 一次 Run 只能绑定一张预算预留；未知费用保留原额直至对账。
CREATE TABLE IF NOT EXISTS public.gateway_budget_reservations (
    id uuid PRIMARY KEY,
    run_id uuid NOT NULL UNIQUE REFERENCES public.agent_runs(id) ON DELETE RESTRICT,
    account_id uuid NOT NULL REFERENCES public.gateway_budget_accounts(id) ON DELETE RESTRICT,
    reserved_amount numeric(30,12) NOT NULL,
    status varchar(16) NOT NULL DEFAULT 'reserved',
    confirmed_cost numeric(30,12),
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT ck_gateway_reservation_positive CHECK (reserved_amount > 0),
    CONSTRAINT ck_gateway_reservation_status CHECK (status IN ('reserved','settled')),
    CONSTRAINT ck_gateway_reservation_settlement CHECK (
        (status = 'reserved' AND confirmed_cost IS NULL) OR
        (status = 'settled' AND confirmed_cost >= 0)
    )
);

CREATE INDEX IF NOT EXISTS ix_gateway_budget_reservations_account
    ON public.gateway_budget_reservations(account_id);

-- 迟到回执修正既更新账户累计值，也追加不可变变动记录供运营核对。
CREATE TABLE IF NOT EXISTS public.gateway_budget_adjustments (
    id uuid PRIMARY KEY,
    reservation_id uuid NOT NULL REFERENCES public.gateway_budget_reservations(id) ON DELETE RESTRICT,
    previous_cost numeric(30,12) NOT NULL,
    confirmed_cost numeric(30,12) NOT NULL,
    delta numeric(30,12) NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT ck_gateway_budget_adjustment_costs CHECK (previous_cost >= 0 AND confirmed_cost >= 0),
    CONSTRAINT ck_gateway_budget_adjustment_delta CHECK (delta = confirmed_cost - previous_cost)
);

CREATE INDEX IF NOT EXISTS ix_gateway_budget_adjustments_reservation
    ON public.gateway_budget_adjustments(reservation_id);

-- 回滚前须先完成未结预留对账，再按 adjustments、reservations、accounts 顺序删除。
