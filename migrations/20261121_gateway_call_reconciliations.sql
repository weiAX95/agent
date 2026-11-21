-- 人工核对与失联裁决同事务写入；证据只存引用，不存用户原文或供应商响应体。
CREATE TABLE IF NOT EXISTS public.gateway_call_reconciliations (
    call_id uuid PRIMARY KEY REFERENCES public.gateway_model_calls(id) ON DELETE RESTRICT,
    reviewer_id uuid NOT NULL REFERENCES public.users(id) ON DELETE RESTRICT,
    evidence_ref varchar(2048) NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);

-- 审计历史不可修改；更正需由新的独立审计流程处理。
CREATE OR REPLACE TRIGGER gateway_call_reconciliation_append_only
BEFORE UPDATE OR DELETE ON public.gateway_call_reconciliations
FOR EACH ROW EXECUTE FUNCTION public.gateway_reject_ledger_mutation();
