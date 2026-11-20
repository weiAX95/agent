-- 历史价格与供应商回执只允许追加；纠错必须写新版本或新回执。
CREATE OR REPLACE FUNCTION public.gateway_reject_ledger_mutation()
RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'gateway ledger rows are append-only';
END;
$$ LANGUAGE plpgsql;

-- 重复执行时替换触发器定义，不放宽已有账本的写入边界。
CREATE OR REPLACE TRIGGER gateway_price_append_only
BEFORE UPDATE OR DELETE ON public.gateway_price_versions
FOR EACH ROW EXECUTE FUNCTION public.gateway_reject_ledger_mutation();

CREATE OR REPLACE TRIGGER gateway_usage_append_only
BEFORE UPDATE OR DELETE ON public.gateway_usage_events
FOR EACH ROW EXECUTE FUNCTION public.gateway_reject_ledger_mutation();
