-- Bidirectional (LONG/SHORT) trading: widen the strategy role and the order/fill side
-- vocabularies.  SHORT opens a short and COVER closes one; the order sides gain
-- SELL_SHORT and BUY_TO_COVER so a ledger row never infers whether an order opens or
-- closes from the broker word alone.  position_cycles/positions.direction and
-- signals.action already allow SHORT, and the order purposes (ENTRY, STRATEGY_EXIT, ...)
-- are position-effect neutral, so no other constraint changes.
--
-- Relaxing a CHECK constraint is done with SQLite's documented in-transaction
-- ``writable_schema`` edit rather than a table rebuild: a rebuild would have to recreate
-- the append-only ``fills`` triggers and every foreign key that points at these tables,
-- while this edit preserves all existing rows, indexes, triggers and foreign keys.
-- ``PRAGMA writable_schema = RESET`` reloads the schema so the widened constraints apply
-- to this connection immediately.
PRAGMA writable_schema = ON;

UPDATE sqlite_schema
SET sql = replace(sql, 'role IN (''BUY'',''SELL'')', 'role IN (''BUY'',''SELL'',''SHORT'',''COVER'')')
WHERE type = 'table' AND name = 'strategy_instances';

UPDATE sqlite_schema
SET sql = replace(sql, 'side IN (''BUY'',''SELL'')', 'side IN (''BUY'',''SELL'',''SELL_SHORT'',''BUY_TO_COVER'')')
WHERE type = 'table' AND name IN ('order_intents', 'broker_orders', 'fills');

PRAGMA writable_schema = RESET;
