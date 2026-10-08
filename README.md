# mike_trade

`mike_trade` is a complete, production-grade, operator-supervised stock trading application built in Python 3.12+ for U.S. equity markets.

The application is structured as a modular single-engine process paired with a separate read-only monitoring dashboard. It manages data ingestion, candle/indicator calculation, strategy evaluation, risk controls, and protective order coverage for 1–10 operator-selected symbols.

---

## Execution Modes & Mode Isolation

`mike_trade` supports four distinct execution modes:

1. **`BACKTEST`**: Deterministic historical bar replay with no broker connections.
2. **`SHADOW`**: Real-time market data evaluation against persistent simulated ledgers.
3. **`PAPER`**: Integrates with the Alpaca Paper Trading API using paper environment credentials.
4. **`LIVE`**: Integrates with the Alpaca Live Trading API.

> [!IMPORTANT]
> **Live Mode Safety Gating**: Live trading mode (`LIVE`) is strictly isolated and gated. Importing modules, querying system status, or running test suites will **never** trigger live execution or submit broker orders. Live operations require explicit operator configuration (`enable_live_trading=True`) and CLI confirmation.

---

## Quickstart & Operator Surface

To initialize the environment and run diagnostic health checks:

```bash
# 1. Display available operator commands
make help

# 2. Install reproducible dependencies
make install

# 3. Run environment & system diagnostic health checks
make doctor

# 4. Print application version
make version
```

---

## Documentation & Architecture

For detailed design specifications and architectural decisions, refer to:
- [System Architecture Specification](docs/ARCHITECTURE.md)
- [ADR 0001: Modular Single-Engine Architecture](docs/ADR/0001-modular-single-engine.md)

---

## Security & Environment Configuration

All API keys, secrets, and credentials must be supplied via environment variables or untracked local configuration files. Secrets are never hardcoded or committed to version control.
