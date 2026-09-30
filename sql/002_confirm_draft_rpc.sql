-- =============================================================
-- 002_confirm_draft_rpc.sql
-- Postgres function: confirm_draft(p_session_id, p_items, p_total)
-- Atomically converts an open draft into an order.
-- Idempotent: double-call returns the existing order.
-- =============================================================

CREATE OR REPLACE FUNCTION confirm_draft(
    p_session_id uuid,
    p_items      jsonb,
    p_total      numeric
)
RETURNS SETOF orders
LANGUAGE plpgsql
SECURITY DEFINER
AS $$
DECLARE
    v_draft       order_drafts%ROWTYPE;
    v_session     sessions%ROWTYPE;
    v_order_id    uuid;
BEGIN
    -- 1. Lock the open draft for this session
    SELECT * INTO v_draft
    FROM order_drafts
    WHERE session_id = p_session_id
      AND status = 'open'
    FOR UPDATE;

    IF NOT FOUND THEN
        RAISE EXCEPTION 'no_open_draft'
            USING DETAIL = 'No open draft found for session ' || p_session_id;
    END IF;

    IF jsonb_array_length(v_draft.items) = 0 THEN
        RAISE EXCEPTION 'empty_draft'
            USING DETAIL = 'Cannot confirm an empty draft';
    END IF;

    -- Idempotent: already confirmed, return existing order
    IF v_draft.order_id IS NOT NULL THEN
        RETURN QUERY SELECT * FROM orders WHERE id = v_draft.order_id;
        RETURN;
    END IF;

    -- 2. Load session
    SELECT * INTO v_session FROM sessions WHERE id = p_session_id;

    IF NOT FOUND THEN
        RAISE EXCEPTION 'session_not_found'
            USING DETAIL = 'Session ' || p_session_id || ' does not exist';
    END IF;

    IF v_session.expires_at < now() THEN
        RAISE EXCEPTION 'session_expired'
            USING DETAIL = 'Session ' || p_session_id || ' has expired';
    END IF;

    -- 3. Insert order (with phone from customer if not a guest)
    INSERT INTO orders (
        restaurant_id, customer_id, table_number,
        items, total_amount, status, customer_phone
    )
    SELECT
        v_session.restaurant_id,
        v_session.customer_id,
        v_session.table_number,
        p_items,
        p_total,
        'pending',
        c.phone
    FROM (SELECT phone FROM customers WHERE id = v_session.customer_id) c
    RETURNING id INTO v_order_id;

    -- Guest fallback (customer_id IS NULL)
    IF v_order_id IS NULL THEN
        INSERT INTO orders (
            restaurant_id, customer_id, table_number,
            items, total_amount, status
        ) VALUES (
            v_session.restaurant_id,
            v_session.customer_id,
            v_session.table_number,
            p_items,
            p_total,
            'pending'
        )
        RETURNING id INTO v_order_id;
    END IF;

    -- 4. Mark draft confirmed
    UPDATE order_drafts
    SET status   = 'confirmed',
        order_id = v_order_id
    WHERE id = v_draft.id;

    -- 5. Return the new order row
    RETURN QUERY SELECT * FROM orders WHERE id = v_order_id;
END;
$$;
