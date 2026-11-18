-- M3 账本表必须在 M2 Run 表完成迁移后执行；不修改旧消息与 checkpoint。
CREATE TABLE IF NOT EXISTS public.gateway_price_versions (
    id varchar(128) PRIMARY KEY,
    provider varchar(64) NOT NULL,
    model varchar(128) NOT NULL,
    currency varchar(3) NOT NULL,
    input_per_million numeric(24,6) NOT NULL,
    output_per_million numeric(24,6) NOT NULL,
    cached_input_per_million numeric(24,6) NOT NULL,
    effective_at timestamptz NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT ck_gateway_price_nonnegative CHECK (
        input_per_million >= 0 AND output_per_million >= 0 AND cached_input_per_million >= 0
    )
);

-- 一次 provider 重试是一条新的物理调用记录，不能复用 call_id。
CREATE TABLE IF NOT EXISTS public.gateway_model_calls (
    id uuid PRIMARY KEY,
    run_id uuid NOT NULL REFERENCES public.agent_runs(id) ON DELETE RESTRICT,
    owner_id uuid NOT NULL REFERENCES public.users(id) ON DELETE RESTRICT,
    provider varchar(64) NOT NULL,
    model varchar(128) NOT NULL,
    status varchar(16) NOT NULL DEFAULT 'started',
    trace_id varchar(128),
    upstream_request_id varchar(128),
    started_at timestamptz NOT NULL DEFAULT now(),
    finished_at timestamptz,
    CONSTRAINT ck_gateway_call_status CHECK (
        status IN ('started','succeeded','failed','cancelled','timed_out','unknown')
    )
);

CREATE INDEX IF NOT EXISTS ix_gateway_calls_run ON public.gateway_model_calls(run_id);

-- 同一外部回执只能入账一次；未知 token 与费用以 NULL 保存。
CREATE TABLE IF NOT EXISTS public.gateway_usage_events (
    id uuid PRIMARY KEY,
    call_id uuid NOT NULL REFERENCES public.gateway_model_calls(id) ON DELETE RESTRICT,
    event_key varchar(128) NOT NULL,
    status varchar(16) NOT NULL,
    input_tokens bigint,
    output_tokens bigint,
    cached_input_tokens bigint,
    price_version_id varchar(128) REFERENCES public.gateway_price_versions(id) ON DELETE RESTRICT,
    currency varchar(3),
    cost numeric(30,12),
    source varchar(32) NOT NULL,
    is_estimated boolean NOT NULL DEFAULT false,
    created_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT uq_gateway_usage_receipt UNIQUE (call_id, event_key),
    CONSTRAINT ck_gateway_usage_status CHECK (
        status IN ('succeeded','failed','cancelled','timed_out','unknown')
    ),
    CONSTRAINT ck_gateway_input_tokens CHECK (input_tokens IS NULL OR input_tokens >= 0),
    CONSTRAINT ck_gateway_output_tokens CHECK (output_tokens IS NULL OR output_tokens >= 0),
    CONSTRAINT ck_gateway_cached_tokens CHECK (cached_input_tokens IS NULL OR cached_input_tokens >= 0),
    CONSTRAINT ck_gateway_cached_within_input CHECK (
        input_tokens IS NULL OR cached_input_tokens IS NULL OR cached_input_tokens <= input_tokens
    ),
    CONSTRAINT ck_gateway_cost_price CHECK (
        cost IS NULL OR (cost >= 0 AND price_version_id IS NOT NULL)
    )
);

CREATE INDEX IF NOT EXISTS ix_gateway_usage_call ON public.gateway_usage_events(call_id);

-- 回滚顺序：确认无待结算调用后，usage_events → model_calls → price_versions。
