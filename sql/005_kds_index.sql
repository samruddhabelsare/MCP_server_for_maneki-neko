-- 005_kds_index.sql
-- Composite partial index for the KDS (Kitchen Display System) active orders query.
-- The KDS displays active tickets in chronological order (earliest first: created_at ASC).
-- list_active_orders() queries:
--     SELECT * FROM orders
--     WHERE restaurant_id = :restaurant_id
--       AND status IN ('pending', 'preparing', 'ready')
--     ORDER BY created_at ASC;
--
-- Postgres requires the WHERE clause to imply the partial index predicate for the index to be chosen.
-- By aligning the columns to (restaurant_id, created_at ASC) with the exact active status predicate,
-- Postgres satisfies the filter and the sort in a single index scan with zero disk sorting.

CREATE INDEX IF NOT EXISTS idx_orders_active_kds
    ON orders (restaurant_id, created_at ASC)
    WHERE status IN ('pending', 'preparing', 'ready');

-- Analytics: sales_summary queries all orders for a restaurant ordered newest first.
CREATE INDEX IF NOT EXISTS idx_orders_restaurant_created
    ON orders (restaurant_id, created_at DESC);

-- Verification query (run with EXPLAIN ANALYZE in Supabase SQL editor):
-- EXPLAIN ANALYZE
-- SELECT * FROM orders
-- WHERE restaurant_id = '00000000-0000-0000-0000-000000000000'
--   AND status IN ('pending', 'preparing', 'ready')
-- ORDER BY created_at ASC;
-- Expect: "Index Scan using idx_orders_active_kds on orders" (no Sort step).

