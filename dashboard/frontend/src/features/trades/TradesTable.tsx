/** Shared trade journal table + filter bar, used by both the account journal
 * (source=account) and the research run trade list (source=backtest). */

import type { TradeFilters, TradeListResponse, TradeRecord } from "../../api/client";
import { Pagination } from "../../components/ui";
import { dateTimeEt, money, percent, quantity } from "../../lib/format";

export const DEFAULT_TRADE_FILTERS: TradeFilters = {
  page: 1,
  pageSize: 25,
  sort: "entry_timestamp",
  direction: "desc",
};

export function TradeFilterBar({
  filters,
  onPatch,
  availableEntryStrategies,
  availableExitReasons,
}: {
  filters: TradeFilters;
  onPatch: (patch: Partial<TradeFilters>) => void;
  availableEntryStrategies: string[];
  availableExitReasons: string[];
}) {
  return (
    <div className="filter-bar">
      <label>
        Symbol
        <input
          value={filters.symbol ?? ""}
          onChange={(event) => onPatch({ symbol: event.target.value.toUpperCase() || undefined })}
          placeholder="All"
          size={8}
        />
      </label>
      <label>
        Entry strategy
        <select value={filters.entryStrategy ?? ""} onChange={(event) => onPatch({ entryStrategy: event.target.value || undefined })}>
          <option value="">All</option>
          {availableEntryStrategies.map((name) => (
            <option key={name}>{name}</option>
          ))}
        </select>
      </label>
      <label>
        Exit
        <select value={filters.exitFilter ?? ""} onChange={(event) => onPatch({ exitFilter: event.target.value || undefined })}>
          <option value="">All</option>
          {availableExitReasons.map((name) => (
            <option key={name}>{name}</option>
          ))}
        </select>
      </label>
      <label>
        Result
        <select value={filters.outcome ?? ""} onChange={(event) => onPatch({ outcome: event.target.value || undefined })}>
          <option value="">All</option>
          <option value="WIN">Winning</option>
          <option value="LOSS">Losing</option>
        </select>
      </label>
      <label>
        Status
        <select value={filters.positionStatus ?? ""} onChange={(event) => onPatch({ positionStatus: event.target.value || undefined })}>
          <option value="">All</option>
          <option value="OPEN">OPEN</option>
          <option value="PARTIALLY_CLOSED">PARTIALLY_CLOSED</option>
          <option value="CLOSED">CLOSED</option>
        </select>
      </label>
      <label>
        Sort
        <select value={filters.sort ?? "entry_timestamp"} onChange={(event) => onPatch({ sort: event.target.value })}>
          <option value="entry_timestamp">Entry time</option>
          <option value="exit_timestamp">Exit time</option>
          <option value="symbol">Symbol</option>
          <option value="realized_pnl">P&L</option>
          <option value="holding_duration">Holding time</option>
        </select>
      </label>
      <button type="button" className="button" onClick={() => onPatch({ direction: filters.direction === "asc" ? "desc" : "asc" })}>
        {filters.direction === "asc" ? "Ascending" : "Descending"}
      </button>
    </div>
  );
}

function tradeText(value: string | null): string {
  if (value === null || value === undefined || value === "") return "UNKNOWN";
  return value;
}

export function TradesTable({
  list,
  filters,
  onPatch,
  loading,
  emptyMessage,
  onOpen,
}: {
  list: TradeListResponse | undefined;
  filters: TradeFilters;
  onPatch: (patch: Partial<TradeFilters>) => void;
  loading: boolean;
  emptyMessage: string;
  onOpen: (trade: TradeRecord) => void;
}) {
  const items = list?.items ?? [];
  return (
    <>
      <div className="table-wrap">
        <table className="data-table trades-table">
          <thead>
            <tr>
              <th>Entry (ET)</th>
              <th>Symbol</th>
              <th>Mode</th>
              <th>Entry strategy</th>
              <th className="right">Entry</th>
              <th className="right">Exit</th>
              <th className="right">Qty</th>
              <th className="right">Realized P&L</th>
              <th className="right">Return</th>
              <th>Exit reason</th>
              <th>Status</th>
            </tr>
          </thead>
          <tbody>
            {items.map((trade) => {
              const pnl = trade.realized_pnl === null ? null : Number(trade.realized_pnl);
              return (
                <tr
                  key={trade.trade_id}
                  className="row-expandable"
                  tabIndex={0}
                  onClick={() => onOpen(trade)}
                  onKeyDown={(event) => {
                    if (event.key === "Enter") onOpen(trade);
                  }}
                >
                  <td>{trade.entry_timestamp === null ? <span className="unavailable">Unavailable</span> : dateTimeEt(trade.entry_timestamp)}</td>
                  <td className="cell-symbol">{trade.symbol}</td>
                  <td>{trade.execution_mode}</td>
                  <td>
                    {trade.entry_strategy_id === null ? (
                      <span className="unavailable">UNKNOWN</span>
                    ) : (
                      <>
                        {trade.entry_strategy_id}
                        {trade.entry_strategy_version ? <span className="cell-sub">v{trade.entry_strategy_version}</span> : null}
                      </>
                    )}
                  </td>
                  <td className="right">{money(trade.entry_price)}</td>
                  <td className="right">{money(trade.exit_price)}</td>
                  <td className="right">{quantity(trade.filled_quantity)}</td>
                  <td className={`right ${pnl === null ? "" : pnl >= 0 ? "text-positive" : "text-negative"}`}>
                    {money(trade.realized_pnl, true)}
                  </td>
                  <td className="right">{percent(trade.realized_return_percent)}</td>
                  <td>
                    {trade.exit_reason === null || trade.exit_reason === "" ? (
                      <span className="unavailable">{trade.position_status === "OPEN" ? "Open" : "UNKNOWN"}</span>
                    ) : (
                      tradeText(trade.exit_reason)
                    )}
                  </td>
                  <td>
                    <span className={`pill pill-neutral status-${trade.position_status.toLowerCase()}`}>
                      {trade.position_status}
                    </span>
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
      {loading ? <p className="hint">Loading trades…</p> : null}
      {!loading && list !== undefined && items.length === 0 ? <p className="hint">{emptyMessage}</p> : null}
      {list !== undefined && list.total > 0 ? (
        <Pagination
          page={list.page}
          pages={list.pages}
          total={list.total}
          onPage={(page) => onPatch({ page })}
          pageSize={list.page_size}
          onPageSize={(size) => onPatch({ pageSize: size, page: 1 })}
        />
      ) : null}
    </>
  );
}
