# Dashboard — Read-Only Operator UI

The dashboard is a **separate read-only process** serving a **compiled**
React/TypeScript/Vite bundle through FastAPI. It never places orders,
never mutates configuration, and never writes to the database. It stays up
when the engine fails or is stopped and independently reports the engine
`OFFLINE` from heartbeat age.

Implementation: `mike_trade/dashboard/server.py` (process factory and
static hosting), `mike_trade/dashboard/api.py` (read service and GET-only
router), frontend source in `dashboard/frontend/` (`package.json`,
`vite.config.ts`).

## 1. Two processes, one database (read-only)

- The engine is the single authoritative writer of the SQLite WAL
  database. The dashboard opens the same database **read-only**
  (`ReadStore`: read-only URI, `query_only`, write-denying authorizer, one
  `BEGIN DEFERRED` snapshot per request) and renders through existing
  projections. It never runs migrations and never imports broker,
  market-data, or research code.
- A middleware answers every non-`GET`/`HEAD` method with `405` (including
  unknown paths), so no write surface can exist by accident.
- Engine-to-dashboard communication is WAL reads plus a heartbeat row
  only. There is no shared in-process state, no RPC that mutates engine
  state, and no dashboard-to-engine order path. By architecture the
  dashboard cannot reach execution, brokers, the controls inbox, or config
  mutations (`docs/ARCHITECTURE.md` §3.5).

## 2. Build and deploy (no Vite dev server in production)

```sh
cd dashboard/frontend
npm install          # Node >= 22.12 (see dashboard/frontend/package.json engines)
npm run build        # tsc --noEmit && vite build  ->  dashboard/frontend/dist/
npm run typecheck    # tsc --noEmit (frontend type gate)
```

- Production serving is the **compiled build only**: `server.py` serves
  `DEFAULT_STATIC_DIR` (`mike_trade/dashboard/frontend/dist`) with SPA
  fallback to `index.html` for non-`/api` paths. A missing build yields
  `503` for UI routes only; `/api` keeps working.
- The `vite.config.ts` `/api` proxy (`MIKE_TRADE_API_PROXY_TARGET`,
  default `http://127.0.0.1:8765`) is **development only**: it forwards API
  calls to a local FastAPI instance during frontend development and is not
  part of the production build or the deployed dashboard server. Never
  point production at a frontend development server.
- Run locally with `make dashboard-start` (or
  `.venv/bin/python -m mike_trade.dashboard.server`), check with
  `make dashboard-health`, stop with `make dashboard-stop`, inspect with
  `make dashboard`. Build output uses hashed asset filenames
  (`assets/[name]-[hash].js`) with no sourcemaps.

## 3. Systemd unit usage (t.1 artifacts)

- `deploy/systemd/mike-trade-dashboard.service` is an **optional
  future-use template only**: it is never copied to `/etc/systemd/system`,
  never enabled, and never started by this repository. The same holds for
  `deploy/systemd/mike-trade-engine.service`.
- The template runs the FastAPI production entrypoint
  (`python -m mike_trade.dashboard.server` via uvicorn), binds localhost
  by default (`127.0.0.1:8765` via `DashboardSettings`), and reuses the
  operator's normal `.env` (`EnvironmentFile=`); it creates no new logins
  or secret stores and the dashboard needs no broker credentials.
- The unit deliberately has **no lifecycle binding to the engine unit**:
  the dashboard stays up when the engine fails or is stopped.
- Non-loopback binds are refused unless the explicit opt-in
  `MIKE_TRADE_DASHBOARD_ALLOW_NON_LOOPBACK=1` is set (with
  `MIKE_TRADE_DASHBOARD_BIND`); remote exposure must sit behind private
  auth/TLS/VPN/reverse proxy because the dashboard has no authentication.
- Host-service operator commands (all exist): `make service-install`,
  `make service-uninstall`, `make service-status`. These only shell out to
  `systemctl` for ordinary unit operations; they perform no deployment by
  themselves (`mike_trade/controls/service_manager.py`).

## 4. OFFLINE determination and stale values

- Engine `OFFLINE` is **computed by the dashboard from heartbeat age
  against the configured threshold using its injected clock**
  (`compute_engine_state` in `mike_trade/dashboard/api.py`). The
  `service_health.status` column written by the engine is never trusted
  for this. Defaults: `heartbeat_stale_seconds = 15`,
  `heartbeat_offline_seconds = 60`.
- `make health` applies the same rule from the CLI: the engine is
  `ONLINE` only while the heartbeat is fresh; beyond the offline
  threshold it reports `OFFLINE` (exit `5`) with per-issue remediation
  commands. Stale feeds, unresolved reconciliation drifts, unknown order
  states, and live positions held while offline raise alerts.
- Every dashboard value is a `Sourced` record (`value`, `as_of`, `fresh`,
  `stale_reason`, `provenance`). Values needing an authority (engine,
  broker link, data feed) are `fresh=false` with a `stale_reason`
  whenever that authority is offline, stale, or disconnected; fields with
  no persisted source are `null` with `stale_reason='unavailable'`.
  Component links are read from `service_health` rows named `engine`,
  `broker`, `feed`, `scheduler`, and `persistence`.

## 5. Account and scenario switching

- The `account_id` in dashboard URLs is the persisted `account_scope_id`
  (environment-qualified: `paper:…`, `shadow:…:scenario`, `live:…`).
  Every query is scoped by it; unknown ids return `404`. Switching
  accounts or shadow scenarios therefore switches the entire balance,
  position, order, signal, and P&L view to that scope's ledger —
  **balances are never mixed across accounts or scenarios**.
- Shadow and backtest scopes have no broker link; their ledger is local,
  so broker authority follows engine liveness.

## 6. Panels shown

The read-only API and UI cover: account KPIs, the 1–10 symbol watchlist
with per-symbol status, signals and strategy evaluations, managed
positions (external holdings labeled `UNMANAGED`), orders, fixed and
trailing protective state, entry limits / lockouts / cooldowns, P&L and
account balance/buying power, health and reconciliation (drifts and
`UNMANAGED_HOLDING` items), and recent trade detail. Operator command
hints shown in the UI name only commands that exist in the `Makefile`/CLI
(currently `make doctor` remediations via `OPERATOR_COMMANDS`).

## 7. What the dashboard never does

The dashboard **never places orders, never cancels or replaces orders,
never mutates configuration, never writes to the database, and never
drives the engine**. All order and configuration actions go through the
engine control channel (`make close`, `make close-all`, `make flatten`,
`make pause`, `make entries-disable`, strategy/symbol mutations, and the
live lifecycle commands), each with its own online and confirmation
requirements (see `docs/SAFETY_AND_RISK.md` §6 and §9).
