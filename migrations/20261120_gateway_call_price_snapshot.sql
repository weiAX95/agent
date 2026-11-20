-- 在已部署的 M3 账本上增量添加调用起点的费率快照引用；重复执行安全。
ALTER TABLE public.gateway_model_calls
    ADD COLUMN IF NOT EXISTS price_version_id varchar(128)
    REFERENCES public.gateway_price_versions(id) ON DELETE RESTRICT;
