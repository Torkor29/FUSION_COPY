-- Migration: Add paper trading columns
-- Date: 2026-03-14
-- Description: Adds paper_balance, paper_initial_balance to users
--              and is_settled, settlement_pnl to trades

-- Users table: paper trading balance tracking
ALTER TABLE users ADD COLUMN IF NOT EXISTS paper_balance FLOAT DEFAULT 1000.0;
ALTER TABLE users ADD COLUMN IF NOT EXISTS paper_initial_balance FLOAT DEFAULT 1000.0;

-- Trades table: market resolution settlement
ALTER TABLE trades ADD COLUMN IF NOT EXISTS is_settled BOOLEAN DEFAULT FALSE;
ALTER TABLE trades ADD COLUMN IF NOT EXISTS settlement_pnl FLOAT;
