-- =============================================================
-- 001_drafts_conversations.sql
-- Creates: sessions, order_drafts, conversations, messages
-- All tables use service-role key only; RLS enabled, no public
-- policies (access is enforced in the Python service layer).
-- =============================================================

-- -------------------------------------------------------
-- sessions
-- -------------------------------------------------------
CREATE TABLE IF NOT EXISTS sessions (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    restaurant_id   uuid NOT NULL REFERENCES restaurants(id) ON DELETE CASCADE,
    table_number    integer NOT NULL,
    customer_id     uuid REFERENCES customers(id) ON DELETE SET NULL,
    character       text NOT NULL DEFAULT 'neko',
    expires_at      timestamptz NOT NULL,
    created_at      timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS sessions_restaurant_idx ON sessions(restaurant_id);
CREATE INDEX IF NOT EXISTS sessions_customer_idx   ON sessions(customer_id);
CREATE INDEX IF NOT EXISTS sessions_expires_idx    ON sessions(expires_at);

ALTER TABLE sessions ENABLE ROW LEVEL SECURITY;

-- -------------------------------------------------------
-- order_drafts
-- One open draft per session at a time.
-- status: 'open' | 'confirmed' | 'cancelled'
-- -------------------------------------------------------
CREATE TABLE IF NOT EXISTS order_drafts (
    id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    session_id    uuid NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    restaurant_id uuid NOT NULL REFERENCES restaurants(id) ON DELETE CASCADE,
    items         jsonb NOT NULL DEFAULT '[]'::jsonb,
    status        text NOT NULL DEFAULT 'open'
                      CHECK (status IN ('open','confirmed','cancelled')),
    order_id      uuid REFERENCES orders(id) ON DELETE SET NULL,
    created_at    timestamptz NOT NULL DEFAULT now(),
    updated_at    timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS order_drafts_session_idx ON order_drafts(session_id);
CREATE INDEX IF NOT EXISTS order_drafts_status_idx  ON order_drafts(session_id, status);

ALTER TABLE order_drafts ENABLE ROW LEVEL SECURITY;

-- -------------------------------------------------------
-- conversations
-- -------------------------------------------------------
CREATE TABLE IF NOT EXISTS conversations (
    id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    session_id    uuid NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    restaurant_id uuid NOT NULL REFERENCES restaurants(id) ON DELETE CASCADE,
    created_at    timestamptz NOT NULL DEFAULT now()
);

CREATE UNIQUE INDEX IF NOT EXISTS conversations_session_unique ON conversations(session_id);

ALTER TABLE conversations ENABLE ROW LEVEL SECURITY;

-- -------------------------------------------------------
-- messages
-- role: 'user' | 'assistant' | 'tool'
-- -------------------------------------------------------
CREATE TABLE IF NOT EXISTS messages (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    conversation_id uuid NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    role            text NOT NULL CHECK (role IN ('user','assistant','tool')),
    content         text NOT NULL,
    tool_name       text,
    tool_call_id    text,
    created_at      timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS messages_conversation_idx ON messages(conversation_id, created_at);

ALTER TABLE messages ENABLE ROW LEVEL SECURITY;

-- -------------------------------------------------------
-- Helper: auto-update order_drafts.updated_at
-- -------------------------------------------------------
CREATE OR REPLACE FUNCTION set_updated_at()
RETURNS TRIGGER LANGUAGE plpgsql AS $$
BEGIN
    NEW.updated_at = now();
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS order_drafts_updated_at ON order_drafts;
CREATE TRIGGER order_drafts_updated_at
    BEFORE UPDATE ON order_drafts
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();
