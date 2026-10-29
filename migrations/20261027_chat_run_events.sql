-- Add a bounded, user-visible execution timeline to assistant messages.
CREATE TABLE IF NOT EXISTS public.chat_run_events (
    id uuid PRIMARY KEY,
    message_id uuid NOT NULL REFERENCES public.chat_messages(id) ON DELETE CASCADE,
    sequence integer NOT NULL CHECK (sequence >= 1),
    stage varchar(16) NOT NULL CHECK (stage IN ('research','answer','memory')),
    status varchar(16) NOT NULL CHECK (status IN ('started','completed','failed','stopped','skipped')),
    summary varchar(240) NOT NULL,
    sources jsonb NOT NULL DEFAULT '[]'::jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT uq_chat_run_events_order UNIQUE (message_id, sequence)
);
