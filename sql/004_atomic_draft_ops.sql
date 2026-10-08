-- =============================================================
-- 004_atomic_draft_ops.sql
-- Postgres function: draft_apply(p_session_id, p_restaurant_id,
--   p_op, p_name, p_qty, p_price, p_instructions)
--
-- Ops: 'add' | 'set' | 'remove' | 'clear'
--
-- Uses SELECT ... FOR UPDATE on the open draft row (creates it if
-- missing) to serialise concurrent mutations in a single round trip.
-- Returns the full draft row after the operation.
--
-- Column names match 001_drafts_conversations.sql:
--   order_drafts(id, session_id, restaurant_id, items, status,
--                order_id, created_at, updated_at)
--   status CHECK ('open','confirmed','cancelled')
-- =============================================================

CREATE OR REPLACE FUNCTION draft_apply(
    p_session_id    uuid,
    p_restaurant_id uuid,
    p_op            text,          -- 'add' | 'set' | 'remove' | 'clear'
    p_name          text    DEFAULT NULL,
    p_qty           integer DEFAULT 1,
    p_price         numeric DEFAULT 0,
    p_instructions  text    DEFAULT ''
)
RETURNS SETOF order_drafts
LANGUAGE plpgsql
SECURITY INVOKER
AS $$
DECLARE
    v_draft     order_drafts%ROWTYPE;
    v_draft_id  uuid;
    v_items     jsonb;
    v_item      jsonb;
    v_found     boolean := false;
    v_idx       integer;
    v_name_lc   text;
BEGIN
    -- 1. Lock or create the open draft for this session
    SELECT * INTO v_draft
    FROM order_drafts
    WHERE session_id    = p_session_id
      AND restaurant_id = p_restaurant_id
      AND status        = 'open'
    FOR UPDATE;

    IF NOT FOUND THEN
        -- Create a new open draft
        INSERT INTO order_drafts (session_id, restaurant_id, items, status)
        VALUES (p_session_id, p_restaurant_id, '[]'::jsonb, 'open')
        RETURNING * INTO v_draft;
    END IF;

    v_draft_id := v_draft.id;
    v_items    := v_draft.items;
    v_name_lc  := lower(p_name);

    -- 2. Apply the operation
    IF p_op = 'clear' THEN
        v_items := '[]'::jsonb;

    ELSIF p_op = 'remove' THEN
        -- Filter out any item whose name matches (case-insensitive)
        SELECT jsonb_agg(elem)
        INTO v_items
        FROM jsonb_array_elements(v_items) AS elem
        WHERE lower(elem->>'name') <> v_name_lc;

        IF v_items IS NULL THEN
            v_items := '[]'::jsonb;
        END IF;

    ELSIF p_op = 'add' THEN
        -- Accumulate qty if item already present, else append
        v_found := false;
        v_items := COALESCE(v_items, '[]'::jsonb);

        SELECT jsonb_agg(
            CASE
                WHEN lower(elem->>'name') = v_name_lc THEN
                    elem
                    || jsonb_build_object('qty', (elem->>'qty')::int + p_qty)
                    || jsonb_build_object('price', p_price)
                    || CASE WHEN p_instructions <> '' THEN
                            jsonb_build_object('instructions', p_instructions)
                       ELSE jsonb_build_object('instructions', elem->>'instructions')
                       END
                ELSE elem
            END
        )
        INTO v_items
        FROM jsonb_array_elements(v_items) AS elem;

        -- Check whether item was found by scanning again (simple)
        SELECT bool_or(lower(elem->>'name') = v_name_lc)
        INTO v_found
        FROM jsonb_array_elements(COALESCE(v_draft.items, '[]'::jsonb)) AS elem;

        IF NOT v_found THEN
            v_items := COALESCE(v_items, '[]'::jsonb) || jsonb_build_array(
                jsonb_build_object(
                    'name', p_name,
                    'qty',  p_qty,
                    'price', p_price,
                    'instructions', p_instructions
                )
            );
        END IF;

    ELSIF p_op = 'set' THEN
        -- Set absolute qty; if p_qty = 0, remove the item
        IF p_qty = 0 THEN
            SELECT jsonb_agg(elem)
            INTO v_items
            FROM jsonb_array_elements(v_items) AS elem
            WHERE lower(elem->>'name') <> v_name_lc;

            IF v_items IS NULL THEN v_items := '[]'::jsonb; END IF;
        ELSE
            -- Update existing or append new
            v_found := false;

            SELECT jsonb_agg(
                CASE
                    WHEN lower(elem->>'name') = v_name_lc THEN
                        elem
                        || jsonb_build_object('qty', p_qty)
                        || jsonb_build_object('price', p_price)
                    ELSE elem
                END
            )
            INTO v_items
            FROM jsonb_array_elements(v_items) AS elem;

            SELECT bool_or(lower(elem->>'name') = v_name_lc)
            INTO v_found
            FROM jsonb_array_elements(COALESCE(v_draft.items, '[]'::jsonb)) AS elem;

            IF NOT v_found THEN
                v_items := COALESCE(v_items, '[]'::jsonb) || jsonb_build_array(
                    jsonb_build_object(
                        'name', p_name,
                        'qty',  p_qty,
                        'price', p_price,
                        'instructions', p_instructions
                    )
                );
            END IF;
        END IF;

    ELSE
        RAISE EXCEPTION 'unknown_op'
            USING DETAIL = 'draft_apply: unknown op ' || p_op;
    END IF;

    -- 3. Persist and return
    UPDATE order_drafts
    SET items      = v_items,
        updated_at = now()
    WHERE id = v_draft_id
    RETURNING * INTO v_draft;

    RETURN NEXT v_draft;
END;
$$;
