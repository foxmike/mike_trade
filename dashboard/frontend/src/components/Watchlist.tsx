import { useEffect, useState } from "react";
import { ApiError, getWatchlist } from "../api/client";

type WatchlistResponse = Awaited<ReturnType<typeof getWatchlist>>;
type WatchRow = Record<string, unknown>;

export interface WatchlistProps {
  accountId: string;
  /** Preloaded rows from App polling; when omitted the table loads itself (GET only). */
  symbols?: WatchRow[] | WatchlistResponse;
  onSelectSymbol: (symbol: string) => void;
}

function unwrapSymbols(input: WatchRow[] | WatchlistResponse | undefined): WatchRow[] {
  if (input === undefined) return [];
  if (Array.isArray(input)) return input;
  const record = input as unknown as Record<string, unknown>;
  return Array.isArray(record.symbols) ? (record.symbols as WatchRow[]) : [];
}

// Render Decimal strings and other scalars exactly as received; no float
// arithmetic is performed on money values.
function text(node: unknown): string {
  const inner =
    node !== null && typeof node === "object" && "value" in (node as Record<string, unknown>)
      ? (node as Record<string, unknown>).value
      : node;
  if (inner === null || inner === undefined) return "—";
  if (Array.isArray(inner)) {
    if (inner.length === 0) return "—";
    return inner
      .map((item) =>
        item !== null && typeof item === "object" ? JSON.stringify(item) : String(item),
      )
      .join(", ");
  }
  if (typeof inner === "object") return JSON.stringify(inner);
  return String(inner);
}

function staleFlag(node: unknown): string {
  if (
    node !== null &&
    typeof node === "object" &&
    (node as Record<string, unknown>).fresh === false
  ) {
    const reason = (node as Record<string, unknown>).stale_reason;
    return typeof reason === "string" && reason.length > 0 ? `stale (${reason})` : "stale";
  }
  return "fresh";
}

function innerValue(node: unknown): string | null {
  if (node !== null && typeof node === "object" && "value" in (node as Record<string, unknown>)) {
    const value = (node as Record<string, unknown>).value;
    if (value === null || value === undefined) return null;
    if (typeof value === "object") return JSON.stringify(value);
    return String(value);
  }
  if (node === null || node === undefined) return null;
  if (typeof node === "object") return JSON.stringify(node);
  return String(node);
}

function signalText(node: unknown): string {
  const value =
    node !== null && typeof node === "object" && "value" in (node as Record<string, unknown>)
      ? (node as Record<string, unknown>).value
      : null;
  if (value === null || value === undefined || typeof value !== "object") return text(node);
  const record = value as Record<string, unknown>;
  const disposition =
    typeof record.disposition === "string" ? record.disposition : "—";
  const reason =
    typeof record.disposition_reason === "string" && record.disposition_reason.length > 0
      ? record.disposition_reason
      : "—";
  const action = typeof record.action === "string" ? record.action : "—";
  return `${action} ${disposition} (${reason})`;
}

function evalText(node: unknown): string {
  const value =
    node !== null && typeof node === "object" && "value" in (node as Record<string, unknown>)
      ? (node as Record<string, unknown>).value
      : null;
  if (value === null || value === undefined || typeof value !== "object") return text(node);
  const record = value as Record<string, unknown>;
  const status = typeof record.status === "string" ? record.status : "—";
  const reason = typeof record.reason === "string" ? record.reason : "—";
  return `${status} (${reason})`;
}

export default function Watchlist({ accountId, symbols, onSelectSymbol }: WatchlistProps) {
  const preloaded = unwrapSymbols(symbols);
  const [fetched, setFetched] = useState<WatchRow[] | null>(preloaded.length > 0 ? preloaded : null);
  const [loadError, setLoadError] = useState<string | null>(null);

  // Self-load only when the parent did not supply rows. Read-only GET.
  useEffect(() => {
    const supplied = unwrapSymbols(symbols);
    if (supplied.length > 0) {
      setFetched(supplied);
      setLoadError(null);
      return;
    }
    if (symbols !== undefined) {
      setFetched([]);
      return;
    }
    let cancelled = false;
    const controller = new AbortController();
    (async () => {
      try {
        const body = (await (getWatchlist as (
          id: string,
          opts?: { signal?: AbortSignal },
        ) => Promise<WatchlistResponse>)(accountId, { signal: controller.signal })) as unknown as Record<
          string,
          unknown
        >;
        if (cancelled || controller.signal.aborted) return;
        setFetched(Array.isArray(body.symbols) ? (body.symbols as WatchRow[]) : []);
        setLoadError(null);
      } catch (error) {
        if (cancelled || controller.signal.aborted) return;
        if (error instanceof ApiError) setLoadError(error.message);
        else setLoadError(error instanceof Error ? error.message : "Watchlist load failed");
      }
    })();
    return () => {
      cancelled = true;
      controller.abort();
    };
  }, [accountId, symbols]);

  const rows = (fetched ?? preloaded).slice(0, 10);

  if (loadError !== null && rows.length === 0) {
    return (
      <div className="watchlist-empty">
        <p>
          Watchlist unavailable: {loadError}. Retry with <code>make doctor</code>.
        </p>
      </div>
    );
  }

  if (rows.length === 0) {
    return (
      <div className="watchlist-empty">
        <p>No symbols in the watchlist for this account.</p>
      </div>
    );
  }

  return (
    <div className="watchlist-wrap">
      <table className="watchlist-table">
        <thead>
          <tr>
            <th scope="col">Symbol</th>
            <th scope="col">Price / bid / ask</th>
            <th scope="col">Data age / stale</th>
            <th scope="col">Phase</th>
            <th scope="col">Strategies (BUY / SELL)</th>
            <th scope="col">Triggers / evaluation / signal</th>
            <th scope="col">Position (qty / cost / time)</th>
            <th scope="col">Budget / commitment / planned loss</th>
            <th scope="col">Entries / streak / lockout / cooldown / hold</th>
            <th scope="col">Protection</th>
            <th scope="col">Stops (broker HWM / stepped anchor, desired / confirmed)</th>
            <th scope="col">P&amp;L</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => {
            const symbol = typeof row.symbol === "string" ? row.symbol : "—";
            const confirmedQty = innerValue(row.confirmed_quantity);
            const protectionQty = innerValue(row.protection_quantity);
            const coverageGap =
              confirmedQty !== null && protectionQty !== null && confirmedQty !== protectionQty;
            const rowStale =
              row.data_stale === true ||
              (row.price !== null &&
                typeof row.price === "object" &&
                (row.price as Record<string, unknown>).fresh === false);
            return (
              <tr
                key={symbol}
                className={rowStale ? "watch-row watch-row-stale" : "watch-row"}
                onClick={() => {
                  if (symbol !== "—") onSelectSymbol(symbol);
                }}
              >
                <td>
                  <button
                    type="button"
                    className="symbol-link"
                    onClick={(event) => {
                      event.stopPropagation();
                      if (symbol !== "—") onSelectSymbol(symbol);
                    }}
                  >
                    {symbol}
                  </button>
                  <span className="row-sub">configured: {text(row.configured)}</span>
                  {coverageGap ? (
                    <span className="coverage-warning" role="alert">
                      COVERAGE GAP: protection qty {text(row.protection_quantity)} differs from
                      confirmed qty {text(row.confirmed_quantity)}
                    </span>
                  ) : null}
                </td>
                <td>
                  <span className="field">
                    <span className="field-label">price</span> {text(row.price)}{" "}
                    <span className="fresh-flag">{staleFlag(row.price)}</span>
                  </span>
                  <span className="field">
                    <span className="field-label">bid</span> {text(row.bid)}{" "}
                    <span className="fresh-flag">{staleFlag(row.bid)}</span>
                  </span>
                  <span className="field">
                    <span className="field-label">ask</span> {text(row.ask)}{" "}
                    <span className="fresh-flag">{staleFlag(row.ask)}</span>
                  </span>
                  <span className="field">
                    <span className="field-label">feed</span> {text(row.feed)}
                  </span>
                </td>
                <td>
                  <span className="field">
                    <span className="field-label">data age (s)</span>{" "}
                    {text(row.data_age_seconds)}
                  </span>
                  <span className="field">
                    <span className="field-label">data stale</span> {text(row.data_stale)}
                  </span>
                </td>
                <td>
                  <span className="field">
                    <span className="field-label">composite phase</span> {text(row.phase)}{" "}
                    <span className="fresh-flag">{staleFlag(row.phase)}</span>
                  </span>
                </td>
                <td>
                  <span className="field">
                    <span className="field-label">active BUY strategies</span>{" "}
                    {text(row.active_buy_strategies)}
                  </span>
                  <span className="field">
                    <span className="field-label">active SELL strategies</span>{" "}
                    {text(row.active_sell_strategies)}
                  </span>
                </td>
                <td>
                  <span className="field">
                    <span className="field-label">next due trigger</span>{" "}
                    {text(row.next_due_trigger)}
                  </span>
                  <span className="field">
                    <span className="field-label">last evaluation</span> {evalText(row.last_evaluation)}
                  </span>
                  <span className="field">
                    <span className="field-label">last signal</span> {signalText(row.last_signal)}
                  </span>
                </td>
                <td>
                  <span className="field">
                    <span className="field-label">confirmed qty</span>{" "}
                    {text(row.confirmed_quantity)}{" "}
                    <span className="fresh-flag">{staleFlag(row.confirmed_quantity)}</span>
                  </span>
                  <span className="field">
                    <span className="field-label">cost basis</span> {text(row.cost_basis)}
                  </span>
                  <span className="field">
                    <span className="field-label">purchase time</span> {text(row.purchase_time)}
                  </span>
                </td>
                <td>
                  <span className="field">
                    <span className="field-label">budget</span> {text(row.symbol_budget)}
                  </span>
                  <span className="field">
                    <span className="field-label">commitment</span> {text(row.commitment)}
                  </span>
                  <span className="field">
                    <span className="field-label">planned loss</span> {text(row.planned_loss)}
                  </span>
                </td>
                <td>
                  <span className="field">
                    <span className="field-label">daily entries</span>{" "}
                    {text(row.daily_entry_count)} / cap {text(row.daily_entry_cap)}
                  </span>
                  <span className="field">
                    <span className="field-label">loss streak</span> {text(row.loss_streak)}
                  </span>
                  <span className="field">
                    <span className="field-label">lockout</span> {text(row.lockout_active)}
                    {text(row.lockout_until) !== "—" ? ` until ${text(row.lockout_until)}` : ""}
                  </span>
                  <span className="field">
                    <span className="field-label">loss reset mode</span>{" "}
                    {text(row.loss_reset_mode)}
                  </span>
                  <span className="field">
                    <span className="field-label">cooldown until</span>{" "}
                    {text(row.cooldown_until)}
                  </span>
                  <span className="field">
                    <span className="field-label">next eligible</span>{" "}
                    {text(row.next_eligible_at)}
                  </span>
                  <span className="field">
                    <span className="field-label">minimum-hold remaining (s)</span>{" "}
                    {text(row.min_hold_remaining_seconds)}
                  </span>
                  <span className="field">
                    <span className="field-label">entry cutoff</span> {text(row.entry_cutoff)}
                  </span>
                </td>
                <td>
                  <span className="field">
                    <span className="field-label">protection mode</span>{" "}
                    {text(row.protection_mode)}
                  </span>
                  <span className="field">
                    <span className="field-label">protection status</span>{" "}
                    {text(row.protection_status)}{" "}
                    <span className="fresh-flag">{staleFlag(row.protection_status)}</span>
                  </span>
                  <span className="field">
                    <span className="field-label">protection price</span>{" "}
                    {text(row.protection_price)}
                  </span>
                  <span className="field">
                    <span className="field-label">protection quantity</span>{" "}
                    {text(row.protection_quantity)}
                  </span>
                  <span className="field">
                    <span className="field-label">last protection update</span>{" "}
                    {text(row.last_protection_update)}
                  </span>
                </td>
                <td>
                  <span className="field">
                    <span className="field-label">broker HWM</span>{" "}
                    {text(row.native_broker_hwm)}{" "}
                    <span className="fresh-flag">{staleFlag(row.native_broker_hwm)}</span>
                  </span>
                  <span className="field">
                    <span className="field-label">stepped anchor</span>{" "}
                    {text(row.stepped_anchor)}
                  </span>
                  <span className="field">
                    <span className="field-label">desired stop</span> {text(row.desired_stop)}
                  </span>
                  <span className="field">
                    <span className="field-label">confirmed stop</span>{" "}
                    {text(row.confirmed_stop)}
                  </span>
                </td>
                <td>
                  <span className="field">
                    <span className="field-label">realized today</span>{" "}
                    {text(row.realized_pnl_today)}
                  </span>
                  <span className="field">
                    <span className="field-label">unrealized</span> {text(row.unrealized_pnl)}
                  </span>
                  <span className="field">
                    <span className="field-label">lifetime realized</span>{" "}
                    {text(row.lifetime_realized_pnl)}
                  </span>
                  <span className="field">
                    <span className="field-label">lifetime</span> {text(row.lifetime_pnl)}
                  </span>
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
      <p className="watchlist-note">
        Showing {String(rows.length)} of up to 10 symbols. Row selection is read-only; for
        follow-up run <code>make doctor</code>.
      </p>
    </div>
  );
}
