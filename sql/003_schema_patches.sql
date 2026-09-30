-- =============================================================
-- 003_schema_patches.sql
-- Patches to existing tables to close gaps vs PRD Section 9.
-- Safe to re-run (IF NOT EXISTS / ADD COLUMN IF NOT EXISTS).
-- =============================================================

-- Add bot_id to orders (nullable FK to bots)
ALTER TABLE orders
    ADD COLUMN IF NOT EXISTS bot_id uuid REFERENCES bots(id) ON DELETE SET NULL;

-- Enforce phone uniqueness per restaurant on customers
-- (application must use ON CONFLICT (restaurant_id, phone) DO UPDATE)
ALTER TABLE customers
    DROP CONSTRAINT IF EXISTS customers_restaurant_phone_unique;
ALTER TABLE customers
    ADD CONSTRAINT customers_restaurant_phone_unique
    UNIQUE (restaurant_id, phone);
