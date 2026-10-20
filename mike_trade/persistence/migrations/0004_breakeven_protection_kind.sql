-- Michelle (corrected) fixed-then-breakeven protection: persist the new kind.
--
-- ``trailing_stop_states`` and ``protection_orders`` constrain ``kind`` to the protection kinds
-- that existed when they were last relaxed (0003).  The corrected VWAP Bounce/Break strategy adds
-- FIXED_THEN_BREAKEVEN_STOP (fixed $0.75 stop, then a one-way move to breakeven at +$0.50).
-- Same technique as 0002/0003: relax the CHECK with SQLite's documented in-transaction
-- ``writable_schema`` edit instead of a table rebuild, which would have to recreate every foreign
-- key and trigger that references these tables.  All rows, indexes and keys are preserved.
PRAGMA writable_schema = ON;

UPDATE sqlite_schema
SET sql = replace(
  sql,
  'kind IN (''FIXED_STOP'',''BROKER_TRAILING_STOP'',''STEPPED_TRAILING_STOP'',''FIXED_THEN_TRAILING_STOP'')',
  'kind IN (''FIXED_STOP'',''BROKER_TRAILING_STOP'',''STEPPED_TRAILING_STOP'',''FIXED_THEN_TRAILING_STOP'',''FIXED_THEN_BREAKEVEN_STOP'')'
)
WHERE type = 'table' AND name IN ('trailing_stop_states', 'protection_orders');

PRAGMA writable_schema = RESET;
