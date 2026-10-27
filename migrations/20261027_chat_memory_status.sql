-- Run once before deploying the API code. Existing rows have unknown outcome.
ALTER TABLE public.chat_messages
    ADD COLUMN IF NOT EXISTS memory_status varchar(16);

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'public.chat_messages'::regclass
          AND conname = 'ck_chat_messages_memory_status'
    ) THEN
        ALTER TABLE public.chat_messages
            ADD CONSTRAINT ck_chat_messages_memory_status
            CHECK (memory_status IS NULL OR memory_status IN
                   ('saved', 'unchanged', 'skipped', 'failed', 'unknown'));
    END IF;
END $$;
