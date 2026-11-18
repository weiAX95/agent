-- Run 与消息分开记录；唯一键使同一请求无法创建第二个执行事实。
CREATE TABLE IF NOT EXISTS public.agent_runs (
    id uuid PRIMARY KEY,
    owner_id uuid NOT NULL REFERENCES public.users(id) ON DELETE RESTRICT,
    session_id uuid NOT NULL REFERENCES public.chat_sessions(id) ON DELETE RESTRICT,
    request_id uuid NOT NULL,
    assistant_message_id uuid NOT NULL REFERENCES public.chat_messages(id) ON DELETE RESTRICT,
    status varchar(16) NOT NULL DEFAULT 'created',
    max_steps integer NOT NULL,
    steps integer NOT NULL DEFAULT 0,
    recovery_required boolean NOT NULL DEFAULT false,
    deadline_at timestamptz NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    finished_at timestamptz,
    CONSTRAINT uq_agent_runs_request UNIQUE (session_id, request_id),
    CONSTRAINT uq_agent_runs_assistant UNIQUE (assistant_message_id),
    CONSTRAINT ck_agent_runs_status CHECK (status IN ('created','running','succeeded','failed','cancelled','timed_out')),
    CONSTRAINT ck_agent_runs_steps CHECK (max_steps > 0 AND steps >= 0 AND steps <= max_steps)
);

-- 已部署过早期 M2 草稿的测试环境也可重复升级；正式库应核对执行记录。
ALTER TABLE public.agent_runs
    ADD COLUMN IF NOT EXISTS recovery_required boolean NOT NULL DEFAULT false;

-- 回滚：确认不再有运行中 Run 后，DROP TABLE public.agent_runs。
