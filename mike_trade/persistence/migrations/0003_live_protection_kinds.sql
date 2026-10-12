-- PAPER/LIVE protective stops: persist the fixed and fixed-then-trailing (Michelle) kinds.
--
-- ``trailing_stop_states`` is the durable per-cycle stop record the live lifecycle restores
-- from after a restart, and it only admitted the broker-trailing and stepped kinds;
-- ``protection_orders`` lacked the fixed-then-trailing kind.  Like 0002, a CHECK constraint is
-- relaxed with SQLite's documented in-transaction ``writable_schema`` edit rather than a table
-- rebuild, which would have to recreate every foreign key and trigger that references these
-- tables.  All rows, indexes and keys are preserved.
PRAGMA writable_schema = ON;

UPDATE sqlite_schema
SET sql = replace(
  sql,
  'kind IN (''BROKER_TRAILING_STOP'',''STEPPED_TRAILING_STOP'')',
  'kind IN (''FIXED_STOP'',''BROKER_TRAILING_STOP'',''STEPPED_TRAILING_STOP'',''FIXED_THEN_TRAILING_STOP'')'
)
WHERE type = 'table' AND name = 'trailing_stop_states';

UPDATE sqlite_schema
SET sql = replace(
  sql,
  'kind IN (''FIXED_STOP'',''BROKER_TRAILING_STOP'',''STEPPED_TRAILING_STOP'')',
  'kind IN (''FIXED_STOP'',''BROKER_TRAILING_STOP'',''STEPPED_TRAILING_STOP'',''FIXED_THEN_TRAILING_STOP'')'
)
WHERE type = 'table' AND name = 'protection_orders';

PRAGMA writable_schema = RESET;
