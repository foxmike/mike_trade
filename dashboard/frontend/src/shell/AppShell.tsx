/** Persistent application shell: nav rail, global header (account/scenario,
 * trading mode, engine liveness, market session), content outlet, footer.
 * The header's engine pill promotes to OFFLINE client-side once the ticking
 * heartbeat age exceeds the API's offline threshold, without waiting for the
 * next poll — the same rule as the previous dashboard. */

import { useEffect, useState } from "react";
import { NavLink, Outlet, useLocation, useNavigate } from "react-router";
import { useStatus } from "../state/queries";
import { useSelectedAccount } from "../state/AccountContext";
import {
  EngineBadge,
  LinkStatusBadge,
  Loading,
  ModeBadge,
  MarketBadge,
  SessionBadge,
  UnreachableBanner,
} from "../components/ui";
import { ApiError } from "../api/client";
import { dateTimeEt, timeEt, timeUtc } from "../lib/format";

// Mirrors DashboardSettings.heartbeat_offline_seconds (API default).
const OFFLINE_AFTER_SECONDS = 60;

function useTick(intervalMs: number): void {
  const [, setTick] = useState(0);
  useEffect(() => {
    const timer = window.setInterval(() => setTick((tick) => tick + 1), intervalMs);
    return () => window.clearInterval(timer);
  }, [intervalMs]);
}

interface NavItem {
  to: string;
  label: string;
  group: string;
  planned?: boolean;
}

const NAV_ITEMS: NavItem[] = [
  { to: "/operations", label: "Operations", group: "Operations" },
  { to: "/watchlist", label: "Watchlist", group: "Operations" },
  { to: "/account", label: "Account detail", group: "Operations" },
  { to: "/research", label: "Research Lab", group: "Research" },
  { to: "/trades", label: "Trade Explorer", group: "Trades" },
  { to: "/health", label: "Health", group: "System" },
  { to: "/market", label: "Market Monitor", group: "System" },
  { to: "/config", label: "Configuration & Control", group: "System", planned: true },
];

const NAV_GROUP_ORDER = ["Operations", "Research", "Trades", "System"];

function NavRail() {
  const groups = NAV_GROUP_ORDER.map((group) => ({
    group,
    items: NAV_ITEMS.filter((item) => item.group === group),
  }));
  return (
    <nav className="nav-rail" aria-label="Console navigation">
      <div className="nav-brand">
        <span className="brand-mark">MT</span>
        <span className="brand-text">
          MIKE_TRADE
          <small>console</small>
        </span>
      </div>
      {groups.map(({ group, items }) => (
        <div key={group} className="nav-group">
          <div className="nav-group-label">{group}</div>
          {items.map((item) => (
            <NavLink
              key={item.to}
              to={item.to}
              className={({ isActive }) =>
                `nav-item${isActive ? " nav-active" : ""}${item.planned ? " nav-planned" : ""}`
              }
              title={item.planned ? `${item.label} — planned, not yet functional` : item.label}
            >
              <span className="nav-label">{item.label}</span>
              {item.planned ? <span className="nav-soon">soon</span> : null}
            </NavLink>
          ))}
        </div>
      ))}
      <div className="nav-footer">
        Read-only console.
        <br />
        No element submits orders.
      </div>
    </nav>
  );
}

function AccountSelect() {
  const { accounts, accountsLoading, accountsError, selectedAccountId, selectAccount } =
    useSelectedAccount();
  const navigate = useNavigate();

  if (accountsLoading && accounts.length === 0) return <Loading label="Accounts…" />;
  if (accountsError !== null) {
    return (
      <span className="header-account-error">
        Account list unavailable: {accountsError}
      </span>
    );
  }

  const modes = [...new Set(accounts.map((account) => account.mode.toUpperCase()))].sort(
    (a, b) => {
      const order = (mode: string) => (mode === "LIVE" ? 0 : mode === "PAPER" ? 1 : mode === "SHADOW" ? 2 : 3);
      return order(a) - order(b);
    },
  );

  return (
    <select
      className="account-select"
      aria-label="Account / scenario selection"
      value={selectedAccountId ?? ""}
      onChange={(event) => {
        selectAccount(event.target.value);
        // Account-scoped detail routes must not carry over across accounts.
        void navigate("/operations");
      }}
    >
      {modes.map((mode) => (
        <optgroup key={mode} label={mode}>
          {accounts
            .filter((account) => account.mode.toUpperCase() === mode)
            .map((account) => (
              <option key={account.account_id} value={account.account_id}>
                {account.account_id}
                {account.scenario ? ` · ${account.scenario}` : ""}
              </option>
            ))}
        </optgroup>
      ))}
    </select>
  );
}

function Header() {
  const { selectedAccountId } = useSelectedAccount();
  const statusQuery = useStatus(selectedAccountId);
  useTick(1000);

  const status = statusQuery.data ?? null;
  const engineServer = status?.engine_state ?? null;
  const baseAge = status?.heartbeat_age_seconds ?? null;
  const elapsed = statusQuery.dataUpdatedAt > 0 ? (Date.now() - statusQuery.dataUpdatedAt) / 1000 : 0;
  const tickingAge = baseAge === null ? null : Math.max(0, baseAge + elapsed);
  const clientOffline = tickingAge !== null && tickingAge > OFFLINE_AFTER_SECONDS;
  const engine = clientOffline ? "OFFLINE" : (engineServer ?? "—");

  const unreachable =
    statusQuery.isError &&
    statusQuery.error instanceof ApiError &&
    (statusQuery.error.status === null || statusQuery.error.status === undefined);

  const apiErrorText =
    statusQuery.isError && statusQuery.error instanceof Error ? statusQuery.error.message : null;

  const staleWatermark =
    selectedAccountId !== null &&
    status !== null &&
    (clientOffline || status.fresh === false || status.is_stale === true);

  return (
    <header className="global-header">
      <div className="header-cell header-identity">
        <span className="header-title">Operations console</span>
        <AccountSelect />
      </div>
      <div className="header-cell header-pills">
        {selectedAccountId !== null ? <ModeBadge mode={status?.mode ?? null} /> : null}
        {selectedAccountId !== null ? <EngineBadge engine={engine} /> : null}
        {selectedAccountId !== null ? (
          <LinkStatusBadge link={status?.broker} label="BROKER" />
        ) : null}
        {selectedAccountId !== null ? <LinkStatusBadge link={status?.data} label="DATA" /> : null}
        {selectedAccountId !== null ? <MarketBadge market={status?.market ?? null} /> : null}
      </div>
      <div className="header-cell header-clock">
        {status !== null ? (
          <>
            <span className="clock-et">
              {timeEt(status.exchange_clock?.now_et ?? null)} <small>ET</small>
            </span>
            <span className="clock-session">
              <SessionBadge session={status.exchange_clock?.session_status ?? null} />
            </span>
            <span className="clock-utc" title={dateTimeEt(status.server_time)}>
              {timeUtc(status.server_time)} <small>UTC</small>
            </span>
          </>
        ) : (
          <span className="hint">—</span>
        )}
      </div>
      <div className="header-cell header-engine-detail">
        {selectedAccountId !== null ? (
          <>
            <span className="header-kv">
              <span>heartbeat</span>
              <strong>{tickingAge === null ? "—" : `${tickingAge.toFixed(1)}s`}</strong>
            </span>
            <span className="header-kv">
              <span>cutoff</span>
              <strong>
                {status?.next_entry_cutoff?.at_et ?? status?.next_entry_cutoff?.at ?? "—"}
                {status?.next_entry_cutoff?.passed_today ? " (passed)" : ""}
              </strong>
            </span>
            <span className="header-kv">
              <span>entries</span>
              <strong className={status?.entries_allowed ? "text-positive" : "text-negative"}>
                {status === null ? "—" : status.entries_allowed ? "ALLOWED" : "BLOCKED"}
              </strong>
            </span>
          </>
        ) : null}
      </div>
      {unreachable ? <UnreachableBanner /> : null}
      {!unreachable && apiErrorText !== null && selectedAccountId !== null ? (
        <div className="banner banner-error" role="alert">
          <strong>Status unavailable</strong>
          <span>{apiErrorText}</span>
        </div>
      ) : null}
      {staleWatermark ? (
        <div className="stale-watermark" role="status">
          STALE — showing last-known values
        </div>
      ) : null}
    </header>
  );
}

function isAccountRequired(pathname: string): boolean {
  if (
    pathname.startsWith("/research") ||
    pathname.startsWith("/market") ||
    pathname.startsWith("/trades")
  ) {
    return false;
  }
  return true;
}

export default function AppShell() {
  const { selectedAccountId, accountsLoading } = useSelectedAccount();
  const location = useLocation();
  const accountRequired = isAccountRequired(location.pathname);

  return (
    <div className="console-shell">
      <NavRail />
      <div className="console-main">
        <Header />
        <main className="console-content">
          {accountRequired && selectedAccountId === null && accountsLoading ? (
            <Loading label="Loading accounts…" />
          ) : accountRequired && selectedAccountId === null ? (
            <div className="state-block state-empty">
              <p>No account selected.</p>
              <p className="hint">Select an account in the header to begin monitoring operations.</p>
            </div>
          ) : (
            <Outlet />
          )}
        </main>
        <footer className="app-footer">
          <span>
            Read-only console · no control here submits orders or changes configuration · operator
            actions: <code>make doctor</code> · <code>mike-trade doctor</code>
          </span>
        </footer>
      </div>
    </div>
  );
}
