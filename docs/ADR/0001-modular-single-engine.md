# ADR 0001: Modular Single-Engine Process plus Separate Read-Only Dashboard

## Status

Accepted

## Title

Modular single-engine trading process plus a separate read-only dashboard
process, with no microservices or message brokers.

## Context

`mike_trade` is an operator-supervised stock-trading product for roughly 1–10
operator-selected U.S. equities on a single Ubuntu host (primarily the Dell
PowerEdge R730, secondarily the development desktop). It acquires market data
once per symbol, computes shared candles/indicators, evaluates independently
configurable Python BUY/SELL strategy classes, and manages one intentional
long entry plus one complete discretionary exit per position cycle under
fail-closed risk control.

Candidate topologies were: (a) distributed microservices with a message broker
(Kafka/Celery/Redis) and orchestration (Kubernetes); (b) a monolithic
single-file script; (c) a modular single-engine process with a separate
read-only dashboard process backed by one SQLite WAL writer. The product must
stay restart-safe, keep account/portfolio/order mutations serialized, isolate
BACKTEST/SHADOW/PAPER/LIVE credentials and ledgers, and let the operator see
state even when the engine is down — without the operational cost of
distributed state. No external requirements document is required. This record
makes no claim about strategy profitability.

## Decision

Adopt a modular single-engine process plus a separate dashboard process:

1. One trading engine process owns ingestion, candles/indicators, bounded
   strategy evaluation, risk/portfolio policy, execution, controls, and
   persistence. Internals are modules behind `Protocol` ports with
   dependency injection composed only in `app.py`/`runtime.py`.
2. One authoritative SQLite WAL writer per operational database with
   forward-only migrations and append-only trade-affecting events.
3. One separate FastAPI dashboard process serving a compiled React +
   TypeScript + Vite build over a read-only API/projection. The dashboard
   never mutates state, configuration, or orders.
4. No Kafka, Celery, Kubernetes, generic rules engines/DSLs, or throwaway
   script architecture.

## Consequences

Positive:

- Deterministic serialized mutations and a single SQLite writer remove
  cross-service transactions, lock contention, and double-spend races for a
  1–10 symbol workload.
- Restart safety: crash-safe WAL plus append-only journal plus broker-truth
  reconciliation on startup before new entries are permitted.
- Operator supervision preserved: the dashboard stays reachable when the
  engine dies (OFFLINE via heartbeat age, stale values marked) yet cannot
  place orders or mutate config.
- Testability without live risk: domain/strategy/risk stay broker-neutral;
   backtest runs with no broker access; shadow scenarios share one feed with
   isolated ledgers; importing modules or reading status never places orders.

Negative / accepted limits:

- Vertical scaling on one host only; sufficient for 1–10 symbols but not for
  a multi-node or high-symbol-count future without a new ADR.
- In-process concurrency must be managed explicitly (one scheduler, bounded
  evaluation pool, serialized execution coordinator, single writer).
- Dashboard capability is intentionally narrow: status and evidence only, not
  a charting terminal, config editor, or order ticket.

## Alternatives Considered

1. **Microservices with Kafka/Celery (and Kubernetes):** rejected. For 1–10
   symbols under operator supervision, brokers add serialization overhead,
   multi-node failure modes, non-atomic cross-service ledger updates, and
   daemon dependencies, while weakening the single-writer restart-safety
   guarantee.
2. **Monolithic single script / unstructured app:** rejected. Tight coupling
   of strategy, broker, feed, and UI harms testability and risks accidental
   live orders; no ports/adapters or composition root to isolate modes.
3. **Dashboard with write/order powers (or direct DB order triggers):**
   rejected. Violates read-only and fail-closed guarantees and creates an
   unvalidated HTTP-to-order path.
4. **Separate database per concern with multi-writer sync:** rejected.
   Conflicts with one authoritative SQLite writer and broker-truth
   reconciliation; kept as a future option only via a new ADR if scale
   demands it.

Rationale summary: 1–10 symbols, operator-supervised operation,
restart-safety, and a single SQLite writer favor one well-modularized engine
plus one isolated read-only dashboard over microservices or message brokers.
