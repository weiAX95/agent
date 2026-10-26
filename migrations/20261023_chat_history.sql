-- V0.1 chat history. Additive, rerunnable migration; no existing data is deleted.
-- Execute the entire file inside one transaction with lock_timeout configured.
CREATE TABLE IF NOT EXISTS public.chat_sessions (
    id uuid PRIMARY KEY,
    owner_id uuid NOT NULL REFERENCES public.users(id) ON DELETE RESTRICT,
    title varchar(200) NOT NULL DEFAULT '新会话',
    checkpoint_thread_id text NOT NULL,
    legacy_session_id text,
    next_message_seq bigint NOT NULL DEFAULT 1,
    last_message_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    deleted_at timestamptz
);

-- Complete the table partially created through the database GUI.
ALTER TABLE public.chat_sessions
    ALTER COLUMN title SET DEFAULT '新会话',
    ALTER COLUMN next_message_seq SET DEFAULT 1,
    ALTER COLUMN next_message_seq SET NOT NULL,
    ALTER COLUMN created_at SET DEFAULT now(),
    ALTER COLUMN updated_at SET DEFAULT now();

CREATE TABLE IF NOT EXISTS public.chat_messages (
    id uuid PRIMARY KEY,
    session_id uuid NOT NULL REFERENCES public.chat_sessions(id) ON DELETE RESTRICT,
    request_id uuid NOT NULL,
    seq bigint NOT NULL,
    role varchar(16) NOT NULL,
    content text NOT NULL DEFAULT '',
    status varchar(16) NOT NULL,
    error_code varchar(64),
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    finished_at timestamptz
);

DO $$
DECLARE item record;
BEGIN
    FOR item IN SELECT * FROM (VALUES
      ('chat_sessions', 'uq_chat_sessions_thread', 'UNIQUE (checkpoint_thread_id)'),
      ('chat_sessions', 'uq_chat_sessions_legacy', 'UNIQUE (owner_id, legacy_session_id)'),
      ('chat_sessions', 'ck_chat_sessions_seq', 'CHECK (next_message_seq >= 1)'),
      ('chat_sessions', 'ck_chat_sessions_title', 'CHECK (length(btrim(title)) > 0)'),
      ('chat_messages', 'uq_chat_messages_seq', 'UNIQUE (session_id, seq)'),
      ('chat_messages', 'uq_chat_messages_request_role', 'UNIQUE (session_id, request_id, role)'),
      ('chat_messages', 'ck_chat_messages_seq', 'CHECK (seq >= 1)'),
      ('chat_messages', 'ck_chat_messages_role', 'CHECK (role IN (''user'', ''assistant''))'),
      ('chat_messages', 'ck_chat_messages_status', 'CHECK (status IN (''streaming'', ''completed'', ''stopped'', ''failed'', ''interrupted''))'),
      ('chat_messages', 'ck_chat_messages_user_status', 'CHECK (role <> ''user'' OR status = ''completed'')'),
      ('chat_messages', 'ck_chat_messages_finished', 'CHECK ((status = ''streaming'' AND finished_at IS NULL) OR (status <> ''streaming'' AND finished_at IS NOT NULL))')
    ) AS definitions(table_name, constraint_name, definition)
    LOOP
        IF NOT EXISTS (
            SELECT 1 FROM pg_constraint
            WHERE conrelid = format('public.%I', item.table_name)::regclass
              AND conname = item.constraint_name
        ) THEN
            EXECUTE format('ALTER TABLE public.%I ADD CONSTRAINT %I %s',
                item.table_name, item.constraint_name, item.definition);
        END IF;
    END LOOP;
END $$;

CREATE INDEX IF NOT EXISTS ix_chat_sessions_owner_activity
    ON public.chat_sessions(owner_id, last_message_at DESC, id DESC)
    WHERE deleted_at IS NULL AND last_message_at IS NOT NULL;
