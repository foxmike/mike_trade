# Docker Compose operation

The Compose stack runs the trading engine and the read-only dashboard as separate
processes. The dashboard serves the compiled React application and `/api` from the
same port. SQLite, its WAL files, runtime locks, and other state remain in
`state/dev` on the host.

## DEV lifecycle

Start the stack manually:

```bash
make up
make status
make logs
```

`make up` starts services without rebuilding. Build images separately with
`make build`, or build and recreate the stack with `make rebuild`.

Stop the stack while keeping its containers and data:

```bash
make stop
```

Restart stopped containers with `make start`, or restart running services with
`make restart`. Inspect container processes with `make ps`.

Remove containers and the Compose network while retaining persistent data:

```bash
make down
```

`make down` does not pass `--volumes`; bind-mounted host state remains intact.

The dashboard is available at <http://127.0.0.1:8765/>. The API liveness endpoint
is <http://127.0.0.1:8765/api/live>.

Compose reads credentials from the existing `.env` file. It mounts `config/`
read-only at `/app/config` and maps `state/dev/` to `/app/state`. The config and
state locations are container paths, so deployment on another host only requires
changing the relative bind source or using an equivalent volume. Both services
use `restart: "no"`; no host boot integration is installed.

## Research jobs

The `mike-trade-research` service is invoked only for a CLI job. It mounts
`research/` and receives only Alpaca market-data credentials; it has no trading
state mount, broker credentials, published ports, or dependency on the engine.
For example, `make research-build` builds its image and `make research-six
START=... END=... WORKERS=auto` runs a foreground batch. Stop a foreground
batch with Ctrl-C; completed run artifacts remain in `research/`. See
[`HISTORICAL_RESEARCH.md`](HISTORICAL_RESEARCH.md) for job specifications.

The R730 uses `compose.r730.yaml` through its local `COMPOSE_FILE` setting.
That overlay assigns trading services to the inactive `trading` profile, so
plain Compose startup on that host cannot start a trading engine. Research
commands explicitly select `mike-trade-research` and use `--no-deps`.
