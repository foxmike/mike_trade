# Phase I Scheduler Verification — Shared-Feed Fan-Out

## Scenario

`tests/integration/test_scheduler_fanout.py::test_ten_symbols_share_one_broker_data_feed`
proves that 10 watched symbols
(AAPL, MSFT, NVDA, AMZN, GOOGL, META, TSLA, AMD, JPM, SPY) and all of their
strategies share **one** market-data service and **one** provider stream,
not duplicate per-symbol or per-strategy streams.

A deterministic, in-process fake provider (`FakeMarketDataStream`) implements
the provider side of the shared-service contract (`subscribe` / `unsubscribe`
/ `events` / `close` plus a state observer). It counts stream-open
(subscribe) calls and replays scripted trades, quotes and 1-minute bars over
a fixed simulated session window (2026-03-02 13:30 → 13:36 UTC) driven by a
fake clock (`FakeClock`, pinned at session end). One live trade is pushed
through the async stream path; the remaining 50 scripted updates (1 trade +
1 quote + 3 bars per symbol) are replayed through the real service ingest
path. No network, no credentials and no Alpaca endpoints are used.

## Components exercised (real code, public APIs only)

| Phase | Component | How it is wired |
|---|---|---|
| shared feed | `MarketDataService`, `MarketDataCache` | One `engine` consumer + one `audit` observer registered for all 10 symbols × TRADES/QUOTES/BARS; scripted `StreamTrade`/`StreamQuote`/`StreamBar` events ingested; `cache_snapshot` read back |
| i.1 | `StrategyScheduler`, `CANDLE_CLOSED`/`QUOTE_CHANGE` triggers | 10 watched symbols; 3 registrations per symbol across BUY and SELL with mixed triggers (`momentum_continuation` BUY on 1m close, `pullback_bid` BUY on quote change, `breakdown_exit` SELL on 5m close), plus `faulty_probe` (BUY, quote) and `stalled_probe` (SELL, quote) on AAPL — 32 registrations |
| i.2 | `EventBacklog`, `EvaluationPool` | Burst of 10 `MarketDataArrivalEvent`s with 5 critical events injected mid-burst; pool with `max_workers=4`, `max_in_flight=4`, `evaluation_timeout=0.15s` evaluates every due item |
| i.3 | `DecisionRouter` | All 64 evaluation results + 5 critical events submitted; priority/tie-break ordering, revalidation and duplicate suppression exercised |
| i.4 | Serialized decision consumer | A single async consumer behind the router's lock stands in for the engine's decision path (see Scope notes); re-entrancy is measured, and three concurrent `drain()` calls prove serialization |

## Assertions and what each proves

1. **One stream for all 10 symbols** — `stream_open_count == 1`; the single
   subscribe call covers all 30 (symbol, channel) pairs; strategies expose no
   `subscribe`/`connect`/`stream` surface and the provider has no sockets/URLs.
2. **Fan-out** — engine and audit consumers each receive the identical live
   trade object (`first_engine is first_audit`) and finish with 50 queued
   updates each; every cache snapshot has freshness metadata.
3. **Bounded pool** — thread high-water mark ≤ 4 (`max_workers`) and
   therefore < 10 × 32; measured with 24 concurrent 30 ms probe evaluations.
4. **Exactly-once closed bars** — 30 × 1m dues + 10 × 5m dues; re-feeding
   identical candle events, duplicate quotes and 5 control ticks all yield
   zero additional dues; strategy call log shows each (symbol, strategy, side,
   bar) invoked exactly once.
5. **Coalescing** — burst of versions 1..10 drains as a single version-10
   item with `coalesced == 9`; re-putting version 3 returns `False`
   (`discarded == 1`); the surviving latest version yields no scheduler dues;
   a version-3 due submitted after watermark 10 resolves `CANCELLED`, never a
   signal.
6. **Critical delivery** — all 5 scripted fills/stops drain FIFO within their
   classes, reach the consumer first (ahead of all 40 signals) exactly once,
   and the consumer peak concurrency is 1 across three concurrent drains.
7. **Fault isolation** — `faulty_probe` → `ERROR` (detail contains the
   scripted failure), `stalled_probe` → `TIMEOUT`; both recorded
   `NON_ACTIONABLE`, neither ever delivered as BUY.
8. **Determinism** — two full runs produce byte-identical ordered decision
   logs (69 records: 45 forwarded + 24 non-actionable) and identical delivery
   order. Outcome split: 40 SIGNAL / 20 NO_SIGNAL / 2 ERROR / 2 TIMEOUT.

## Exact pytest command

```bash
.venv/bin/python -m pytest tests/integration/test_scheduler_fanout.py -v
```

(Python 3.12 project venv; `asyncio_mode = auto` from `pyproject.toml`.)

## Observed results (2026-10-08 run, both runs identical)

- `stream_open_count = 1`, `subscribed_pair_count = 30`,
  `engine_depth = 50`, `audit_depth = 50`, cache snapshots fresh for 10/10 symbols
- `registrations = 32`
- `n_1m_dues = 30`, `n_5m_dues = 10`, `n_quote_dues = 24`,
  `repeat_candle = 0`, `repeat_5m = 0`, `duplicate_quote = 0`, `control_dues = 0`
- `high_water = 4` (bound 4; 10 × 32 = 320)
- `by_outcome = {SIGNAL: 40, NO_SIGNAL: 20, ERROR: 2, TIMEOUT: 2}`,
  `stale_outcome = CANCELLED`
- `coalesced = 9`, `discarded_final = 1`, `stale_put_accepted = False`,
  `drained_versions = [10]`, `arrival_dues = 0`
- `n_drained_critical = 5` in order
  Fill/AAPL, Fill/MSFT, Fill/NVDA, Stop/TSLA, Stop/AMD;
  `consumer_peak = 1`, `n_delivered = 45`
- Full test wall time ≈ 2 s (limit: 60 s)

## Scope notes (recorded, no production change made)

- The repo contains no `mike_trade/engine.py` and no
  `mike_trade/observability/health.py`; the i.4 assertion target (one
  serialized consumer, never re-entrant) is therefore exercised directly
  against the `DecisionRouter` consumer contract, which is the behavior the
  engine owns downstream.
- The repo contains no strategy registry and no `vwap_bounce`/`vwap_breakdown`
  strategies (only `strategies/indicators/core.py`), so the test uses small
  test-local strategy classes that read only the provided evaluation context,
  registered directly on the scheduler — exactly the fallback the task allows.
- There is no `tests/conftest.py`; the test is fully self-contained.

## Claim boundary

This fixture proves **shared-feed fan-out and scheduler correctness only**:
one provider stream serves all symbols/strategies, scheduling is exactly-once
per closed bar, bursts coalesce, critical events are never lost or reordered
behind signals, faults are isolated, and the decision log is deterministic.
It makes **no claim of strategy profitability** and involves **no broker and
no paper/live access**.
