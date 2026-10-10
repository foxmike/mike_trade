# mike_trade operator targets.
#
# Docker Compose is the primary runtime lifecycle interface. Application
# management and query targets remain thin wrappers around the operator CLI
# ($(PYTHON) -m mike_trade.cli); other recipes use established tooling. No
# provider calls, inline queries, or inline scripting live here. Live confirmation is enforced by
# the CLI itself; Make only forwards CONFIRM_LIVE from the operator
# environment and never enables it by default.
#
# Pass-through operator variables (all empty by default; set them on the
# make command line or via the environment):
#   ACCOUNT, SCENARIO, SYMBOL, STRATEGY, KEY, VALUE, SINCE, UNTIL, LIMIT,
#   FORMAT, OUT, RUN_ID, CONFIRM_LIVE
# See docs/OPERATIONS.md for the operator runbook.

.DEFAULT_GOAL := help

PYTHON ?= .venv/bin/python
CLI := $(PYTHON) -m mike_trade.cli

ACCOUNT ?=
SCENARIO ?=
SYMBOL ?=
STRATEGY ?=
KEY ?=
VALUE ?=
SINCE ?=
UNTIL ?=
LIMIT ?=
FORMAT ?=
OUT ?=
RUN_ID ?=
CONFIRM_LIVE ?=

ACCOUNT_FLAG := $(if $(ACCOUNT),--account $(ACCOUNT))
SCENARIO_FLAG := $(if $(SCENARIO),--scenario $(SCENARIO))
FORMAT_FLAG := $(if $(FORMAT),--format $(FORMAT))
SYMBOL_FLAG := $(if $(SYMBOL),--symbol $(SYMBOL))
STRATEGY_FLAG := $(if $(STRATEGY),--strategy $(STRATEGY))
SINCE_FLAG := $(if $(SINCE),--since $(SINCE))
UNTIL_FLAG := $(if $(UNTIL),--until $(UNTIL))
LIMIT_FLAG := $(if $(LIMIT),--limit $(LIMIT))
RUN_FLAG := $(if $(RUN_ID),--run-id $(RUN_ID))
OUT_FLAG := $(if $(OUT),--output $(OUT))
BASE_FLAGS := $(ACCOUNT_FLAG) $(SCENARIO_FLAG) $(FORMAT_FLAG)
FILTER_FLAGS := $(BASE_FLAGS) $(SYMBOL_FLAG) $(STRATEGY_FLAG) $(SINCE_FLAG) $(UNTIL_FLAG) $(LIMIT_FLAG) $(RUN_FLAG)

.PHONY: help install update test test-unit test-integration test-paper lint typecheck
.PHONY: up up-dashboard start stop restart down down-dashboard build rebuild status logs ps
.PHONY: engine-start engine-stop engine-restart engine-status app-logs

up: ## Start Compose services in the background without rebuilding
	docker compose up -d

up-dashboard: ## Start only the read-only dashboard without the trading engine
	docker compose up -d --no-deps mike-trade-api

start: ## Start existing stopped Compose containers
	docker compose start

stop: ## Stop Compose containers without removing containers or data
	docker compose stop

restart: ## Restart Compose services
	docker compose restart

down: ## Remove Compose containers and network; preserve persistent data
	docker compose down

down-dashboard: ## Stop only the dashboard service
	docker compose stop mike-trade-api

build: ## Build Compose images
	docker compose build

rebuild: ## Build images and recreate Compose services
	docker compose up -d --build --force-recreate

status: ## Show Compose service status
	docker compose ps

logs: ## Follow Compose service logs
	docker compose logs -f

ps: ## Show processes running in Compose containers
	docker compose top

help: ## Show this help message (default goal)
	@echo "mike_trade operational commands (see docs/OPERATIONS.md):"
	@grep -E '^[a-zA-Z0-9_.-]+:.*?## .*$$' $(MAKEFILE_LIST) | sort | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-22s\033[0m %s\n", $$1, $$2}'

install: ## Install locked dependencies and verify the environment
	uv sync --frozen
	CONFIRM_LIVE=$(CONFIRM_LIVE) $(CLI) doctor $(BASE_FLAGS)

update: ## Refresh the dependency lock file and resynchronize the environment
	uv lock --upgrade
	uv sync --frozen

test: ## Run the bounded default test suite (unit plus integration paths)
	$(PYTHON) -m pytest tests/unit tests/integration -q

test-unit: ## Run the unit test path only
	$(PYTHON) -m pytest tests/unit -q

test-integration: ## Run the integration test path only
	$(PYTHON) -m pytest tests/integration -q

test-paper: ## Run paper-credential-gated tests (skipped without paper credentials)
	$(PYTHON) -m pytest -m paper -q

lint: ## Run the configured linter
	uv run --frozen ruff check .

typecheck: ## Run the configured type checker
	uv run --frozen mypy mike_trade

# --- environment and configuration ---

.PHONY: doctor config-show

doctor: ## Run read-only environment and dependency health checks
	CONFIRM_LIVE=$(CONFIRM_LIVE) $(CLI) doctor $(BASE_FLAGS)

config-show: ## Show the effective configuration for the chosen account
	CONFIRM_LIVE=$(CONFIRM_LIVE) $(CLI) config-show $(BASE_FLAGS)

# --- providers and market data ---

.PHONY: providers provider-check broker-check data-check

providers: ## List configured providers
	CONFIRM_LIVE=$(CONFIRM_LIVE) $(CLI) providers $(BASE_FLAGS)

provider-check: ## Check provider connectivity and credentials
	CONFIRM_LIVE=$(CONFIRM_LIVE) $(CLI) provider-check $(BASE_FLAGS)

broker-check: ## Check broker connectivity and account access
	CONFIRM_LIVE=$(CONFIRM_LIVE) $(CLI) broker-check $(BASE_FLAGS)

data-check: ## Check market data freshness and coverage
	CONFIRM_LIVE=$(CONFIRM_LIVE) $(CLI) data-check $(BASE_FLAGS)

# --- strategies ---

.PHONY: strategies strategy-info strategy-check strategy-enable strategy-disable strategy-set

strategies: ## List strategies and their enablement state
	CONFIRM_LIVE=$(CONFIRM_LIVE) $(CLI) strategies $(BASE_FLAGS)

strategy-info: ## Show details for one strategy (STRATEGY=name)
	CONFIRM_LIVE=$(CONFIRM_LIVE) $(CLI) strategy-info $(BASE_FLAGS) $(STRATEGY)

strategy-check: ## Validate one strategy configuration (STRATEGY=name)
	CONFIRM_LIVE=$(CONFIRM_LIVE) $(CLI) strategy-check $(BASE_FLAGS) $(STRATEGY)

strategy-enable: ## Enable one strategy (STRATEGY=name; needs live confirmation on LIVE)
	CONFIRM_LIVE=$(CONFIRM_LIVE) $(CLI) strategy-enable $(BASE_FLAGS) $(STRATEGY)

strategy-disable: ## Disable one strategy (STRATEGY=name; needs live confirmation on LIVE)
	CONFIRM_LIVE=$(CONFIRM_LIVE) $(CLI) strategy-disable $(BASE_FLAGS) $(STRATEGY)

strategy-set: ## Set strategy configuration (STRATEGY or KEY=name, VALUE=config)
	CONFIRM_LIVE=$(CONFIRM_LIVE) $(CLI) strategy-set $(BASE_FLAGS) $(or $(STRATEGY),$(KEY)) $(VALUE)

# --- symbols ---

.PHONY: symbols symbols-add symbols-remove symbols-enable symbols-disable

symbols: ## List symbols and their enablement state
	CONFIRM_LIVE=$(CONFIRM_LIVE) $(CLI) symbols $(BASE_FLAGS)

symbols-add: ## Add symbols to the universe (SYMBOL=sym, VALUE=optional detail)
	CONFIRM_LIVE=$(CONFIRM_LIVE) $(CLI) symbols-add $(BASE_FLAGS) $(SYMBOL) $(VALUE)

symbols-remove: ## Remove symbols from the universe (SYMBOL=sym)
	CONFIRM_LIVE=$(CONFIRM_LIVE) $(CLI) symbols-remove $(BASE_FLAGS) $(SYMBOL) $(VALUE)

symbols-enable: ## Enable symbols for new entries (SYMBOL=sym)
	CONFIRM_LIVE=$(CONFIRM_LIVE) $(CLI) symbols-enable $(BASE_FLAGS) $(SYMBOL) $(VALUE)

symbols-disable: ## Disable symbols for new entries (SYMBOL=sym)
	CONFIRM_LIVE=$(CONFIRM_LIVE) $(CLI) symbols-disable $(BASE_FLAGS) $(SYMBOL) $(VALUE)

# --- engine lifecycle ---

.PHONY: engine-start engine-stop engine-restart engine-status

engine-start: ## Start the engine for the chosen account (native CLI)
	CONFIRM_LIVE=$(CONFIRM_LIVE) $(CLI) start $(BASE_FLAGS)

engine-stop: ## Stop the engine loop; positions and protective stops are left in place
	CONFIRM_LIVE=$(CONFIRM_LIVE) $(CLI) stop $(BASE_FLAGS)

engine-restart: ## Restart the engine for the chosen account
	CONFIRM_LIVE=$(CONFIRM_LIVE) $(CLI) restart $(BASE_FLAGS)

engine-status: ## Show engine status for the chosen account
	CONFIRM_LIVE=$(CONFIRM_LIVE) $(CLI) status $(BASE_FLAGS)

# --- dashboard and health ---

.PHONY: dashboard dashboard-start dashboard-stop dashboard-health health

dashboard: ## Show dashboard service status
	CONFIRM_LIVE=$(CONFIRM_LIVE) $(CLI) dashboard $(BASE_FLAGS)

dashboard-start: ## Start the dashboard service
	CONFIRM_LIVE=$(CONFIRM_LIVE) $(CLI) dashboard-start $(BASE_FLAGS)

dashboard-stop: ## Stop the dashboard service
	CONFIRM_LIVE=$(CONFIRM_LIVE) $(CLI) dashboard-stop $(BASE_FLAGS)

dashboard-health: ## Check dashboard service health
	CONFIRM_LIVE=$(CONFIRM_LIVE) $(CLI) dashboard-health $(BASE_FLAGS)

health: ## Show engine health (ONLINE or OFFLINE) with alert details
	CONFIRM_LIVE=$(CONFIRM_LIVE) $(CLI) health $(BASE_FLAGS)

# --- shadow mode ---

.PHONY: shadow-start shadow-stop shadow-list shadow-create shadow-reset

shadow-start: ## Start a shadow scenario run
	CONFIRM_LIVE=$(CONFIRM_LIVE) $(CLI) shadow-start $(BASE_FLAGS)

shadow-stop: ## Stop a shadow scenario run
	CONFIRM_LIVE=$(CONFIRM_LIVE) $(CLI) shadow-stop $(BASE_FLAGS)

shadow-list: ## List shadow scenarios
	CONFIRM_LIVE=$(CONFIRM_LIVE) $(CLI) shadow-list $(BASE_FLAGS)

shadow-create: ## Create a shadow scenario (VALUE=scenario definition)
	CONFIRM_LIVE=$(CONFIRM_LIVE) $(CLI) shadow-create $(BASE_FLAGS) $(VALUE)

shadow-reset: ## Reset a shadow scenario to its initial state
	CONFIRM_LIVE=$(CONFIRM_LIVE) $(CLI) shadow-reset $(BASE_FLAGS)

# --- paper mode ---

.PHONY: paper-start paper-stop paper-status

paper-start: ## Start paper trading for the chosen account
	CONFIRM_LIVE=$(CONFIRM_LIVE) $(CLI) paper-start $(BASE_FLAGS)

paper-stop: ## Stop paper trading for the chosen account
	CONFIRM_LIVE=$(CONFIRM_LIVE) $(CLI) paper-stop $(BASE_FLAGS)

paper-status: ## Show paper trading status
	CONFIRM_LIVE=$(CONFIRM_LIVE) $(CLI) paper-status $(BASE_FLAGS)

# --- live mode ---

.PHONY: live-preflight live-enable live-start live-disable

live-preflight: ## Run live trading preflight checks (refuses on failure)
	CONFIRM_LIVE=$(CONFIRM_LIVE) $(CLI) live-preflight $(BASE_FLAGS)

live-enable: ## Enable live trading after preflight (needs live confirmation)
	CONFIRM_LIVE=$(CONFIRM_LIVE) $(CLI) live-enable $(BASE_FLAGS)

live-start: ## Start live trading after preflight (needs live confirmation)
	CONFIRM_LIVE=$(CONFIRM_LIVE) $(CLI) live-start $(BASE_FLAGS)

live-disable: ## Disable live trading immediately (risk-reducing; no confirmation hurdle)
	CONFIRM_LIVE=$(CONFIRM_LIVE) $(CLI) live-disable $(BASE_FLAGS)

# --- entry controls ---

.PHONY: pause resume pause-symbol resume-symbol entries-disable entries-enable

pause: ## Pause new entries account-wide (risk-reducing; protection keeps running)
	CONFIRM_LIVE=$(CONFIRM_LIVE) $(CLI) pause $(BASE_FLAGS)

resume: ## Resume new entries account-wide (needs live confirmation on LIVE)
	CONFIRM_LIVE=$(CONFIRM_LIVE) $(CLI) resume $(BASE_FLAGS)

pause-symbol: ## Pause new entries for one symbol (SYMBOL=sym; risk-reducing)
	CONFIRM_LIVE=$(CONFIRM_LIVE) $(CLI) pause-symbol $(BASE_FLAGS) $(SYMBOL)

resume-symbol: ## Resume new entries for one symbol (SYMBOL=sym)
	CONFIRM_LIVE=$(CONFIRM_LIVE) $(CLI) resume-symbol $(BASE_FLAGS) $(SYMBOL)

entries-disable: ## Disable entries until re-enabled (risk-reducing; survives resume of pause)
	CONFIRM_LIVE=$(CONFIRM_LIVE) $(CLI) entries-disable $(BASE_FLAGS)

entries-enable: ## Re-enable entries (needs live confirmation on LIVE)
	CONFIRM_LIVE=$(CONFIRM_LIVE) $(CLI) entries-enable $(BASE_FLAGS)

# --- order actions ---

.PHONY: close close-all flatten

close: ## Close one position (SYMBOL=sym; refused while the engine is offline)
	CONFIRM_LIVE=$(CONFIRM_LIVE) $(CLI) close $(BASE_FLAGS) $(SYMBOL)

close-all: ## Close all managed positions (refused while the engine is offline)
	CONFIRM_LIVE=$(CONFIRM_LIVE) $(CLI) close-all $(BASE_FLAGS)

flatten: ## Flatten the book including protective handling (refused while offline)
	CONFIRM_LIVE=$(CONFIRM_LIVE) $(CLI) flatten $(BASE_FLAGS)

# --- trailing protection ---

.PHONY: trail-status trail-history trail-policy-show trail-policy-set trail-reconcile

trail-status: ## Show trailing stop state per position
	CONFIRM_LIVE=$(CONFIRM_LIVE) $(CLI) trail-status $(BASE_FLAGS)

trail-history: ## Show trailing stop replacement history
	CONFIRM_LIVE=$(CONFIRM_LIVE) $(CLI) trail-history $(BASE_FLAGS)

trail-policy-show: ## Show the active trailing stop policy
	CONFIRM_LIVE=$(CONFIRM_LIVE) $(CLI) trail-policy-show $(BASE_FLAGS)

trail-policy-set: ## Set the trailing stop policy (KEY or VALUE=policy)
	CONFIRM_LIVE=$(CONFIRM_LIVE) $(CLI) trail-policy-set $(BASE_FLAGS) $(or $(KEY),$(VALUE))

trail-reconcile: ## Reconcile trailing stops against broker state
	CONFIRM_LIVE=$(CONFIRM_LIVE) $(CLI) trail-reconcile $(BASE_FLAGS)

# --- entry limits and cooldowns ---

.PHONY: entry-limits entry-limits-reset cooldown-status

entry-limits: ## Show entry limits and lockout state
	CONFIRM_LIVE=$(CONFIRM_LIVE) $(CLI) entry-limits $(BASE_FLAGS)

entry-limits-reset: ## Reset entry limits and lockouts (needs live confirmation on LIVE)
	CONFIRM_LIVE=$(CONFIRM_LIVE) $(CLI) entry-limits-reset $(BASE_FLAGS)

cooldown-status: ## Show per-symbol cooldown state
	CONFIRM_LIVE=$(CONFIRM_LIVE) $(CLI) cooldown-status $(BASE_FLAGS)

# --- fixed stops ---

.PHONY: stop-status stops-audit

stop-status: ## Show fixed protective stop state per position
	CONFIRM_LIVE=$(CONFIRM_LIVE) $(CLI) stop-status $(BASE_FLAGS)

stops-audit: ## Audit protective stops for orphans and unsupported placements
	CONFIRM_LIVE=$(CONFIRM_LIVE) $(CLI) stops-audit $(BASE_FLAGS)

# --- portfolio reads and reconciliation ---

.PHONY: positions orders balance budget reconcile

positions: ## List managed positions (external holdings are marked UNMANAGED)
	CONFIRM_LIVE=$(CONFIRM_LIVE) $(CLI) positions $(BASE_FLAGS)

orders: ## List orders for the chosen account
	CONFIRM_LIVE=$(CONFIRM_LIVE) $(CLI) orders $(BASE_FLAGS)

balance: ## Show account balance and buying power
	CONFIRM_LIVE=$(CONFIRM_LIVE) $(CLI) balance $(BASE_FLAGS)

budget: ## Show risk budget usage and reservations
	CONFIRM_LIVE=$(CONFIRM_LIVE) $(CLI) budget $(BASE_FLAGS)

reconcile: ## Reconcile local state against the broker and report drifts
	CONFIRM_LIVE=$(CONFIRM_LIVE) $(CLI) reconcile $(BASE_FLAGS)

# --- history and logs ---

.PHONY: signals evaluations events app-logs

signals: ## Query bounded signal history (SYMBOL/SINCE/UNTIL/LIMIT/RUN_ID filters)
	CONFIRM_LIVE=$(CONFIRM_LIVE) $(CLI) signals $(FILTER_FLAGS)

evaluations: ## Query bounded strategy evaluation history
	CONFIRM_LIVE=$(CONFIRM_LIVE) $(CLI) evaluations $(FILTER_FLAGS)

events: ## Query bounded domain event history
	CONFIRM_LIVE=$(CONFIRM_LIVE) $(CLI) events $(FILTER_FLAGS)

app-logs: ## Read bounded structured application logs via CLI
	CONFIRM_LIVE=$(CONFIRM_LIVE) $(CLI) logs $(FILTER_FLAGS)

# --- reports ---

.PHONY: report report-daily report-symbol report-strategy report-pairs report-shadow report-comparison report-export

report: ## Build the trade cycle report for the chosen scope
	CONFIRM_LIVE=$(CONFIRM_LIVE) $(CLI) report $(BASE_FLAGS)

report-daily: ## Build the per-day trade report for the chosen scope
	CONFIRM_LIVE=$(CONFIRM_LIVE) $(CLI) report-daily $(BASE_FLAGS)

report-symbol: ## Build the trade report for one symbol (SYMBOL=sym)
	CONFIRM_LIVE=$(CONFIRM_LIVE) $(CLI) report-symbol $(BASE_FLAGS) $(SYMBOL)

report-strategy: ## Build the trade report for one strategy (STRATEGY=name)
	CONFIRM_LIVE=$(CONFIRM_LIVE) $(CLI) report-strategy $(BASE_FLAGS) $(STRATEGY)

report-pairs: ## Build the per-pair trade summary for the chosen scope
	CONFIRM_LIVE=$(CONFIRM_LIVE) $(CLI) report-pairs $(BASE_FLAGS)

report-shadow: ## Build the shadow scenario trade report
	CONFIRM_LIVE=$(CONFIRM_LIVE) $(CLI) report-shadow $(BASE_FLAGS)

report-comparison: ## Compare trade reports across two explicitly scoped accounts
	CONFIRM_LIVE=$(CONFIRM_LIVE) $(CLI) report-comparison $(BASE_FLAGS)

report-export: ## Export the trade report (VALUE=dataset, OUT=file path)
	CONFIRM_LIVE=$(CONFIRM_LIVE) $(CLI) report-export $(BASE_FLAGS) $(VALUE) $(OUT_FLAG)

# --- backtests ---

.PHONY: backtest backtest-status backtest-report backtest-list research-build research-data research-six research-backtest research-batch research-results research-sync-r730
.PHONY: research-data-plan research-data-preflight research-data-acquire research-data-status research-data-verify

WORKERS ?= auto
JOBS ?=
RESEARCH_REMOTE ?= mike@192.168.254.147
RESEARCH_REMOTE_ROOT ?= /home/mike/dev/mike/stocks/python/mike_trade
RESEARCH_SSH ?= ssh
RESEARCH_UNIVERSE ?= config/research_universe_100_v1.json
RESEARCH_DATA_ROOT ?= /home/mike/dev/mike/stocks/python/mike_trade/research/data
RESEARCH_PYTHON ?= /home/mike/dev/mike/stocks/python/mike_trade/.venv/bin/python
RESEARCH_ENV ?= /home/mike/dev/mike/stocks/python/mike_trade/.env
RESEARCH_WORKERS ?= 1
RESEARCH_RPM ?= 20
RESEARCH_UNIVERSE_FLAGS := --universe "$(RESEARCH_UNIVERSE)" --root "$(RESEARCH_DATA_ROOT)"

research-build: ## Build only the manually invoked research image
	docker compose build mike-trade-research

research-sync-r730: ## Incrementally copy immutable research data/results from DEV to the R730
	flock -s research/data/.catalog-write.lock rsync -a --ignore-existing --exclude='/failure-check/' --exclude='/duplicate-write-check/' --exclude='/final-parallel-check/' -e "$(RESEARCH_SSH)" research/ "$(RESEARCH_REMOTE):$(RESEARCH_REMOTE_ROOT)/research/"

backtest: ## Run a backtest (VALUE=dataset or manifest reference)
	CONFIRM_LIVE=$(CONFIRM_LIVE) $(CLI) backtest $(BASE_FLAGS) $(VALUE)

backtest-status: ## Show backtest run status
	CONFIRM_LIVE=$(CONFIRM_LIVE) $(CLI) backtest-status $(BASE_FLAGS)

backtest-report: ## Show a backtest report (STRATEGY=name, VALUE=optional detail)
	CONFIRM_LIVE=$(CONFIRM_LIVE) $(CLI) backtest-report $(BASE_FLAGS) $(STRATEGY) $(VALUE)

backtest-list: ## List recorded backtest runs
	CONFIRM_LIVE=$(CONFIRM_LIVE) $(CLI) backtest-list $(BASE_FLAGS)

research-data: ## Acquire/reuse historical bars (SYMBOLS=... START=... END=...)
	docker compose run --rm --no-deps mike-trade-research research-data acquire --symbols "$(SYMBOLS)" --start "$(START)" --end "$(END)"

research-data-plan: ## Show frozen 100-symbol acquisition plan without downloading
	PYTHONPATH="$(CURDIR)" "$(RESEARCH_PYTHON)" -m mike_trade.cli research-data plan $(RESEARCH_UNIVERSE_FLAGS)

research-data-preflight: ## Check disk, credentials and one bounded Alpaca IEX request
	@set -a; . "$(RESEARCH_ENV)"; set +a; PYTHONPATH="$(CURDIR)" "$(RESEARCH_PYTHON)" -m mike_trade.cli research-data preflight $(RESEARCH_UNIVERSE_FLAGS)

research-data-acquire: ## Operator-started resumable 100-symbol acquisition (RESEARCH_WORKERS=1|2)
	@set -a; . "$(RESEARCH_ENV)"; set +a; PYTHONPATH="$(CURDIR)" "$(RESEARCH_PYTHON)" -m mike_trade.cli research-data acquire-universe $(RESEARCH_UNIVERSE_FLAGS) --workers "$(RESEARCH_WORKERS)" --requests-per-minute "$(RESEARCH_RPM)"

research-data-status: ## Show 100-symbol progress, failures and coverage totals
	PYTHONPATH="$(CURDIR)" "$(RESEARCH_PYTHON)" -m mike_trade.cli research-data status $(RESEARCH_UNIVERSE_FLAGS)

research-data-verify: ## Verify completed universe manifests, checksums and Parquet consistency
	PYTHONPATH="$(CURDIR)" "$(RESEARCH_PYTHON)" -m mike_trade.cli research-data verify $(RESEARCH_UNIVERSE_FLAGS)

research-six: ## Run the six VWAP lifecycle focus experiments (START=... END=... WORKERS=auto|N)
	docker compose run --rm --no-deps mike-trade-research research-six --start "$(START)" --end "$(END)" --workers "$(WORKERS)" --results /app/research/final-six

research-backtest: ## Run one VWAP lifecycle backtest (SYMBOL=... STRATEGY=... START=... END=...)
	docker compose run --rm --no-deps mike-trade-research research-backtest --symbol "$(SYMBOL)" --strategy "$(STRATEGY)" --start "$(START)" --end "$(END)"

research-batch: ## Run a JSON experiment batch (JOBS=...json WORKERS=auto|N)
	docker compose run --rm --no-deps -v "$(abspath $(JOBS)):/app/research/jobs.json:ro" mike-trade-research research-batch --jobs /app/research/jobs.json --workers "$(WORKERS)"

research-results: ## List persisted research results
	docker compose run --rm --no-deps mike-trade-research research-results --results /app/research/final-six

# --- cache and database ---

.PHONY: cache-status cache-refresh db-check db-backup db-restore-dry-run

cache-status: ## Show market data cache status
	CONFIRM_LIVE=$(CONFIRM_LIVE) $(CLI) cache-status $(BASE_FLAGS)

cache-refresh: ## Refresh the market data cache
	CONFIRM_LIVE=$(CONFIRM_LIVE) $(CLI) cache-refresh $(BASE_FLAGS)

db-check: ## Check database integrity and migration state
	CONFIRM_LIVE=$(CONFIRM_LIVE) $(CLI) db-check $(BASE_FLAGS)

db-backup: ## Create an online database backup with a manifest sidecar
	CONFIRM_LIVE=$(CONFIRM_LIVE) $(CLI) db-backup $(BASE_FLAGS) $(or $(OUT),$(VALUE))

db-restore-dry-run: ## Verify a backup without restoring it (no writes)
	CONFIRM_LIVE=$(CONFIRM_LIVE) $(CLI) db-restore-dry-run $(BASE_FLAGS) $(or $(OUT),$(VALUE))

# --- host services ---

.PHONY: service-install service-uninstall service-status

service-install: ## Install systemd units for the engine and dashboard
	CONFIRM_LIVE=$(CONFIRM_LIVE) $(CLI) service-install $(BASE_FLAGS)

service-uninstall: ## Remove systemd units for the engine and dashboard
	CONFIRM_LIVE=$(CONFIRM_LIVE) $(CLI) service-uninstall $(BASE_FLAGS)

service-status: ## Show systemd unit status for engine and dashboard
	CONFIRM_LIVE=$(CONFIRM_LIVE) $(CLI) service-status $(BASE_FLAGS)
