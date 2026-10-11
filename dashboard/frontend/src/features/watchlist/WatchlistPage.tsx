/** Strategy watchlist page. Primary columns stay dense; the long tail of
 * budget/stop/lockout fields expands per row. Rows marked stale by the API
 * never present as active data. */

import { Fragment, useState } from "react";
import { Link } from "react-router";
import { useWatchlist } from "../../state/queries";
import { useSelectedAccount } from "../../state/AccountContext";
import { EmptyState, ErrorBanner, Loading, PageHeader, Section, StaleFlag } from "../../components/ui";
import { staleReason, sourcedText } from "../../lib/sourced";
import { dateTimeEt, MISSING } from "../../lib/format";

type WatchRow = Record<string, unknown>;

function field(row: WatchRow, key: string): string {
  const node = row[key];
  return sourcedText(node);
}

function staleOf(row: WatchRow, key: string): string | null {
  const node = row[key];
  return node !== null && typeof node === "object" && (node as Record<string, unknown>).fresh === false
    ? staleReason(node) ?? ""
    : null;
}

function rowIsStale(row: WatchRow): boolean {
  return (
    row.data_stale === true ||
    staleOf(row, "price") !== null ||
    staleOf(row, "phase") !== null
  );
}

function ExpandedRow({ row }: { row: WatchRow }) {
  const groups: Array<[string, Array<[string, string]>]> = [
    [
      "Budget & position",
      [
        ["Symbol budget", field(row, "symbol_budget")],
        ["Commitment", field(row, "commitment")],
        ["Planned loss", field(row, "planned_loss")],
        ["Confirmed qty", field(row, "confirmed_quantity")],
        ["Cost basis", field(row, "cost_basis")],
        ["Purchase time", field(row, "purchase_time")],
      ],
    ],
    [
      "Entries & gates",
      [
        ["Daily entries / cap", `${field(row, "daily_entry_count")} / ${field(row, "daily_entry_cap")}`],
        ["Loss streak", field(row, "loss_streak")],
        ["Loss reset mode", field(row, "loss_reset_mode")],
        ["Lockout", `${field(row, "lockout_active")}${field(row, "lockout_until") !== MISSING ? ` until ${field(row, "lockout_until")}` : ""}`],
        ["Cooldown until", field(row, "cooldown_until")],
        ["Next eligible", field(row, "next_eligible_at")],
        ["Min-hold remaining (s)", field(row, "min_hold_remaining_seconds")],
        ["Entry cutoff", field(row, "entry_cutoff")],
      ],
    ],
    [
      "Protective stops",
      [
        ["Mode", field(row, "protection_mode")],
        ["Status", field(row, "protection_status")],
        ["Price", field(row, "protection_price")],
        ["Quantity", field(row, "protection_quantity")],
        ["Last update", field(row, "last_protection_update")],
        ["Broker HWM", field(row, "native_broker_hwm")],
        ["Stepped anchor", field(row, "stepped_anchor")],
        ["Desired stop", field(row, "desired_stop")],
        ["Confirmed stop", field(row, "confirmed_stop")],
      ],
    ],
    [
      "P&L",
      [
        ["Realized today", field(row, "realized_pnl_today")],
        ["Unrealized", field(row, "unrealized_pnl")],
        ["Lifetime realized", field(row, "lifetime_realized_pnl")],
        ["Lifetime", field(row, "lifetime_pnl")],
      ],
    ],
  ];
  return (
    <tr className="expanded-row">
      <td colSpan={11}>
        <div className="expanded-grid">
          {groups.map(([title, entries]) => (
            <div key={title} className="expanded-group">
              <h4>{title}</h4>
              <dl className="kv-grid kv-compact">
                {entries.map(([label, value]) => (
                  <div key={label}>
                    <dt>{label}</dt>
                    <dd>{value}</dd>
                  </div>
                ))}
              </dl>
            </div>
          ))}
        </div>
      </td>
    </tr>
  );
}

function WatchlistTable({ symbols }: { symbols: WatchRow[] }) {
  const [expanded, setExpanded] = useState<Set<string>>(new Set());
  const toggle = (symbol: string) => {
    setExpanded((current) => {
      const next = new Set(current);
      if (next.has(symbol)) next.delete(symbol);
      else next.add(symbol);
      return next;
    });
  };
  return (
    <div className="table-wrap">
      <table className="data-table watchlist-table">
        <thead>
          <tr>
            <th>Symbol</th>
            <th className="right">Price</th>
            <th className="right">Bid / ask</th>
            <th className="right">Data age</th>
            <th>Phase</th>
            <th>BUY strategies</th>
            <th>SELL strategies</th>
            <th>Last evaluation</th>
            <th>Last signal</th>
            <th>Protection</th>
            <th className="right">P&L today</th>
          </tr>
        </thead>
        <tbody>
          {symbols.map((row) => {
            const symbol = String(row.symbol ?? MISSING);
            if (symbol === MISSING) return null;
            const stale = rowIsStale(row);
            const coverageGap =
              field(row, "confirmed_quantity") !== MISSING &&
              field(row, "protection_quantity") !== MISSING &&
              field(row, "confirmed_quantity") !== field(row, "protection_quantity");
            const isOpen = expanded.has(symbol);
            const evalNode = (row.last_evaluation as Record<string, unknown> | undefined)?.value;
            const signalNode = (row.last_signal as Record<string, unknown> | undefined)?.value;
            const evalText =
              evalNode !== null && typeof evalNode === "object"
                ? `${String((evalNode as Record<string, unknown>).status ?? "—")} (${String((evalNode as Record<string, unknown>).reason ?? "—")})`
                : sourcedText(row.last_evaluation);
            const signalText =
              signalNode !== null && typeof signalNode === "object"
                ? `${String((signalNode as Record<string, unknown>).action ?? "—")} ${String((signalNode as Record<string, unknown>).disposition ?? "")}`
                : sourcedText(row.last_signal);
            return (
              <Fragment key={symbol}>
                <tr
                  className={`${stale ? "row-stale" : ""} row-expandable`}
                  onClick={() => toggle(symbol)}
                  tabIndex={0}
                  onKeyDown={(event) => {
                    if (event.key === "Enter" || event.key === " ") {
                      event.preventDefault();
                      toggle(symbol);
                    }
                  }}
                >
                  <td>
                    <button
                      type="button"
                      className="link-button expander"
                      aria-expanded={isOpen}
                      onClick={(event) => {
                        event.stopPropagation();
                        toggle(symbol);
                      }}
                      title="Show / hide full symbol detail fields"
                    >
                      {isOpen ? "▾" : "▸"}
                    </button>{" "}
                    <Link
                      className="cell-link"
                      to={`/symbols/${encodeURIComponent(symbol)}`}
                      onClick={(event) => event.stopPropagation()}
                    >
                      {symbol}
                    </Link>
                    {coverageGap ? (
                      <span className="coverage-warning" role="alert">
                        COVERAGE GAP: protection {field(row, "protection_quantity")} ≠ confirmed{" "}
                        {field(row, "confirmed_quantity")}
                      </span>
                    ) : null}
                  </td>
                  <td className="right">
                    {field(row, "price")}
                    {staleOf(row, "price") !== null ? <StaleFlag reason={staleOf(row, "price")} /> : null}
                  </td>
                  <td className="right">
                    {field(row, "bid")} / {field(row, "ask")}
                  </td>
                  <td className="right">
                    {field(row, "data_age_seconds")}s
                    {row.data_stale === true ? <StaleFlag /> : null}
                  </td>
                  <td>
                    {field(row, "phase")}
                    {staleOf(row, "phase") !== null ? <StaleFlag reason={staleOf(row, "phase")} /> : null}
                  </td>
                  <td className="cell-strategies">{field(row, "active_buy_strategies")}</td>
                  <td className="cell-strategies">{field(row, "active_sell_strategies")}</td>
                  <td>
                    <span className="cell-sub">last</span> {evalText}
                  </td>
                  <td>
                    <span className="cell-sub">last</span> {signalText}
                  </td>
                  <td>
                    {field(row, "protection_status")}
                    {staleOf(row, "protection_status") !== null ? (
                      <StaleFlag reason={staleOf(row, "protection_status")} />
                    ) : null}
                  </td>
                  <td className="right">{field(row, "realized_pnl_today")}</td>
                </tr>
                {isOpen ? <ExpandedRow row={row} /> : null}
              </Fragment>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

export default function WatchlistPage() {
  const { selectedAccountId } = useSelectedAccount();
  const query = useWatchlist(selectedAccountId);

  if (selectedAccountId === null) return null;

  const error = query.isError ? (query.error instanceof Error ? query.error.message : "Watchlist load failed") : null;
  const symbols = query.data?.symbols ?? [];

  return (
    <div className="page">
      <PageHeader
        title="Watchlist"
        subtitle={`${selectedAccountId} · monitored symbols, strategy assignments and evaluation state · timestamps in America/New_York`}
      />
      {error !== null && symbols.length === 0 ? (
        <ErrorBanner
          title="Watchlist unavailable"
          message={error}
          hint="Retry with make doctor."
        />
      ) : null}
      {query.isPending && !query.isError ? <Loading label="Loading watchlist…" /> : null}
      {symbols.length > 0 ? (
        <Section title={`Symbols (${symbols.length})`}>
          <WatchlistTable symbols={symbols as unknown as WatchRow[]} />
          <p className="table-note">
            "last" evaluation/signal values are historical, not current activity. Stale rows carry a
            STALE marker and do not imply present engine action.
          </p>
        </Section>
      ) : query.isSuccess ? (
        <Section title="Symbols">
          <EmptyState message="No symbols in the watchlist for this account." />
        </Section>
      ) : null}
    </div>
  );
}
