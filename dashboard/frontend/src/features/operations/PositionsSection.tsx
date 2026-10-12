/** Position detail section (Feature 4): broker-truth positions enriched with
 * cycle lifecycle, entry strategy and protective-stop state from the cockpit
 * payload. Navigation only — trading buttons arrive with the command gateway. */

import { Link } from "react-router";
import type { PositionRow, SymbolPipeline } from "../../api/client";
import { EmptyState, StaleFlag } from "../../components/ui";
import { dateTimeEt, decimal, MISSING, money, quantity } from "../../lib/format";

export default function PositionsSection({
  positions,
  pipelines,
}: {
  positions: PositionRow[];
  pipelines: SymbolPipeline[];
}) {
  if (positions.length === 0) {
    return <EmptyState message="No open positions reported by the broker." />;
  }
  const bySymbol = new Map(pipelines.map((pipeline) => [pipeline.symbol, pipeline]));
  return (
    <div className="table-wrap">
      <table className="data-table">
        <thead>
          <tr>
            <th>Symbol</th>
            <th>Direction</th>
            <th>Lifecycle</th>
            <th className="right">Quantity</th>
            <th className="right">Avg entry</th>
            <th className="right">Mark</th>
            <th className="right">Unrealized P&L</th>
            <th>Entry strategy</th>
            <th>Entry time (ET)</th>
            <th>Protective stop</th>
            <th />
          </tr>
        </thead>
        <tbody>
          {positions.map((position) => {
            const pipeline = bySymbol.get(position.symbol);
            const cycle = pipeline?.cycle ?? null;
            const protection = pipeline?.protections[0] ?? null;
            const pnl = Number(position.unrealized_pl);
            const stale = !position.fresh;
            return (
              <tr key={`${position.symbol}-${position.cycle_id ?? "no-cycle"}`} className={stale ? "row-stale" : undefined}>
                <td className="cell-symbol">
                  <Link className="cell-link" to={`/symbols/${encodeURIComponent(position.symbol)}`}>
                    {position.symbol}
                  </Link>
                  {stale ? <StaleFlag reason={position.stale_reason} /> : null}
                </td>
                <td>{position.direction}</td>
                <td>
                  {cycle?.state ?? position.management}
                  {cycle?.direction ? <span className="cell-sub">{cycle.direction}</span> : null}
                </td>
                <td className="right">{quantity(position.quantity)}</td>
                <td className="right">{decimal(position.average_entry_price)}</td>
                <td className="right">{decimal(position.market_price)}</td>
                <td className={`right ${Number.isFinite(pnl) ? (pnl > 0 ? "text-positive" : pnl < 0 ? "text-negative" : "") : ""}`}>
                  {money(position.unrealized_pl, true)}
                </td>
                <td>
                  {cycle?.entry_strategy_key ?? <span className="unavailable">UNKNOWN</span>}
                  {position.cycle_id ? <span className="cell-sub">{position.cycle_id.slice(0, 18)}…</span> : null}
                </td>
                <td>{cycle?.opened_at ? dateTimeEt(cycle.opened_at) : MISSING}</td>
                <td>
                  {protection === null ? (
                    <span className="pill pill-yellow">NO ACTIVE STOP</span>
                  ) : (
                    <span className="protection-line">
                      <span className="pill pill-green">{protection.status}</span>
                      {protection.kind ?? ""}
                      {protection.stop_price !== null ? ` @ ${decimal(protection.stop_price)}` : ""}
                      {protection.coverage_gap ? <StaleFlag reason="coverage_gap" /> : null}
                    </span>
                  )}
                </td>
                <td>
                  <Link className="link-button" to={`/symbols/${encodeURIComponent(position.symbol)}`}>
                    Inspect
                  </Link>{" "}
                  <Link className="link-button" to="/trades">
                    Journal
                  </Link>
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}
