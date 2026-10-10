import { useEffect, useMemo, useState } from "react";
import {
  getResearchRuns, getResearchTrade, getResearchTrades, getTrade, getTrades,
  type ResearchRun, type TradeBar, type TradeFilters, type TradeInspection,
  type TradeListResponse, type TradeRecord,
} from "../api/client";

const unavailable = (value: unknown) => value === null || value === undefined || value === "" ? "UNKNOWN" : String(value);
const money = (value: string | null) => value === null ? "—" : Number(value).toFixed(2);
const localTime = (value: string | null) => value === null ? "—" : new Intl.DateTimeFormat("en-US", {
  timeZone: "America/New_York", month: "short", day: "2-digit", hour: "2-digit", minute: "2-digit", second: "2-digit",
}).format(new Date(value));
const duration = (value: string | null) => {
  if (value === null) return "—"; const total = Math.max(0, Number(value));
  const hours = Math.floor(total / 3600); const minutes = Math.floor((total % 3600) / 60);
  return hours > 0 ? `${hours}h ${minutes}m` : `${minutes}m ${Math.floor(total % 60)}s`;
};
const newYorkBoundary = (date: string, end: boolean) => {
  const [year, month, day] = date.split("-").map(Number); const hour = end ? 23 : 0;
  const minute = end ? 59 : 0; const second = end ? 59 : 0;
  const guess = Date.UTC(year!, month! - 1, day!, hour, minute, second);
  const parts = new Intl.DateTimeFormat("en-US", { timeZone: "America/New_York", year: "numeric", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", second: "2-digit", hourCycle: "h23" }).formatToParts(new Date(guess));
  const part = (name: string) => Number(parts.find((item) => item.type === name)?.value);
  const represented = Date.UTC(part("year"), part("month") - 1, part("day"), part("hour"), part("minute"), part("second"));
  return new Date(guess - (represented - guess)).toISOString();
};
const newYorkDate = (value: string | undefined) => {
  if (!value) return ""; const parts = new Intl.DateTimeFormat("en-US", { timeZone: "America/New_York", year: "numeric", month: "2-digit", day: "2-digit" }).formatToParts(new Date(value));
  const part = (name: string) => parts.find((item) => item.type === name)?.value ?? "";
  return `${part("year")}-${part("month")}-${part("day")}`;
};

export default function TradeJournal({ accountId, accountMode }: { accountId: string; accountMode: string }) {
  const [source, setSource] = useState<"account" | "backtest">("account");
  const [runs, setRuns] = useState<ResearchRun[]>([]); const [runId, setRunId] = useState("");
  const [filters, setFilters] = useState<TradeFilters>({ page: 1, pageSize: 25, sort: "entry_timestamp", direction: "desc" });
  const [list, setList] = useState<TradeListResponse | null>(null); const [selected, setSelected] = useState<TradeInspection | null>(null);
  const [error, setError] = useState<string | null>(null); const [loading, setLoading] = useState(false);

  useEffect(() => { const controller = new AbortController(); getResearchRuns(controller.signal).then((body) => {
    setRuns(body.runs); setRunId((current) => current || body.runs[0]?.run_id || "");
  }).catch(() => undefined); return () => controller.abort(); }, []);

  useEffect(() => { const controller = new AbortController(); setLoading(true); setSelected(null);
    const request = source === "account" ? getTrades(accountId, filters, controller.signal) :
      (runId ? getResearchTrades(runId, filters, controller.signal) : Promise.resolve({ items: [], page: 1, page_size: 25, total: 0, pages: 0, available_entry_strategies: [], available_exit_reasons: [] }));
    request.then((body) => { setList(body); setError(null); }).catch((reason: unknown) => {
      if (!(reason instanceof DOMException && reason.name === "AbortError")) setError(reason instanceof Error ? reason.message : "Unable to load trades");
    }).finally(() => setLoading(false)); return () => controller.abort();
  }, [accountId, filters, runId, source]);

  const choose = (trade: TradeRecord) => { setError(null); const request = source === "account" ? getTrade(accountId, trade.trade_id) : getResearchTrade(runId, trade.trade_id);
    request.then(setSelected).catch((reason: unknown) => setError(reason instanceof Error ? reason.message : "Unable to inspect trade")); };
  const patchFilter = (patch: Partial<TradeFilters>) => setFilters((current) => ({ ...current, ...patch, page: patch.page ?? 1 }));
  return <section aria-label="Unified trade journal" className="trades-section">
    <div className="section-heading"><div><h2>Trades</h2><p className="hint">Unified read-only journal · timestamps shown in America/New_York</p></div>
      <div className="source-switch"><button className={source === "account" ? "active" : ""} onClick={() => setSource("account")}>Account ({accountMode})</button>
      <button className={source === "backtest" ? "active" : ""} onClick={() => setSource("backtest")}>Backtest</button></div></div>
    <div className="trade-filters">
      {source === "backtest" ? <label>Research run<select value={runId} onChange={(e) => setRunId(e.target.value)}><option value="">No local runs</option>{runs.map((run) => <option key={run.run_id} value={run.run_id}>{run.run_id.slice(0, 12)} · {run.symbols.join(",")}</option>)}</select></label> : null}
      <label>Symbol<input value={filters.symbol ?? ""} onChange={(e) => patchFilter({ symbol: e.target.value.toUpperCase() })} placeholder="All" /></label>
      <label>From<input type="date" value={newYorkDate(filters.dateFrom)} onChange={(e) => patchFilter({ dateFrom: e.target.value ? newYorkBoundary(e.target.value, false) : undefined })} /></label>
      <label>To<input type="date" value={newYorkDate(filters.dateTo)} onChange={(e) => patchFilter({ dateTo: e.target.value ? newYorkBoundary(e.target.value, true) : undefined })} /></label>
      <label>BUY strategy<select value={filters.entryStrategy ?? ""} onChange={(e) => patchFilter({ entryStrategy: e.target.value || undefined })}><option value="">All</option>{list?.available_entry_strategies.map((x) => <option key={x}>{x}</option>)}</select></label>
      <label>Exit<select value={filters.exitFilter ?? ""} onChange={(e) => patchFilter({ exitFilter: e.target.value || undefined })}><option value="">All</option>{list?.available_exit_reasons.map((x) => <option key={x}>{x}</option>)}</select></label>
      <label>Result<select value={filters.outcome ?? ""} onChange={(e) => patchFilter({ outcome: e.target.value || undefined })}><option value="">All</option><option value="WIN">Winning</option><option value="LOSS">Losing</option></select></label>
      <label>Status<select value={filters.positionStatus ?? ""} onChange={(e) => patchFilter({ positionStatus: e.target.value || undefined })}><option value="">All</option><option>OPEN</option><option>PARTIALLY_CLOSED</option><option>CLOSED</option></select></label>
      <label>Sort<select value={filters.sort} onChange={(e) => patchFilter({ sort: e.target.value })}><option value="entry_timestamp">Entry time</option><option value="exit_timestamp">Exit time</option><option value="symbol">Symbol</option><option value="realized_pnl">P&amp;L</option><option value="holding_duration">Holding time</option></select></label>
      <button onClick={() => patchFilter({ direction: filters.direction === "asc" ? "desc" : "asc" })}>{filters.direction === "asc" ? "Ascending" : "Descending"}</button>
    </div>
    {error ? <div className="banner banner-unreachable" role="alert">{error}</div> : null}
    <div className="trade-table-wrap"><table className="trade-table"><thead><tr><th>Date/time</th><th>Symbol</th><th>Mode</th><th>BUY strategy</th><th>Entry</th><th>Exit</th><th>Quantity</th><th>Realized P&amp;L</th><th>Return %</th><th>Exit reason</th><th>Status</th></tr></thead>
      <tbody>{list?.items.map((trade) => <tr key={trade.trade_id} onClick={() => choose(trade)}><td>{localTime(trade.entry_timestamp)}</td><td><button className="symbol-link">{trade.symbol}</button></td><td>{trade.execution_mode}</td><td>{unavailable(trade.entry_strategy_id)}{trade.entry_strategy_version ? ` v${trade.entry_strategy_version}` : ""}</td><td>{money(trade.entry_price)}</td><td>{money(trade.exit_price)}</td><td>{trade.filled_quantity}</td><td className={Number(trade.realized_pnl) >= 0 ? "positive" : "negative"}>{money(trade.realized_pnl)}</td><td>{trade.realized_return_percent === null ? "—" : `${Number(trade.realized_return_percent).toFixed(2)}%`}</td><td>{unavailable(trade.exit_reason)}</td><td>{trade.position_status}</td></tr>)}</tbody></table></div>
    {loading ? <p className="hint">Loading trades…</p> : list?.total === 0 ? <p className="hint">No trades match these filters.</p> : <div className="pagination"><button disabled={(filters.page ?? 1) <= 1} onClick={() => patchFilter({ page: (filters.page ?? 1) - 1 })}>Previous</button><span>Page {list?.page ?? 1} of {Math.max(1, list?.pages ?? 1)} · {list?.total ?? 0} trades</span><button disabled={(filters.page ?? 1) >= (list?.pages ?? 1)} onClick={() => patchFilter({ page: (filters.page ?? 1) + 1 })}>Next</button></div>}
    {selected ? <TradePanel inspection={selected} onClose={() => setSelected(null)} /> : null}
  </section>;
}

function TradePanel({ inspection, onClose }: { inspection: TradeInspection; onClose: () => void }) {
  const t = inspection.trade; const d = inspection.diagnostics;
  return <div className="trade-panel"><div className="panel-heading"><div><h3>{t.symbol} · {t.position_status}</h3><p>{t.execution_mode} · {t.cycle_id}</p></div><button onClick={onClose}>Close</button></div>
    <dl className="diagnostic-grid">
      <Metric label="Entry" value={money(d.entry_price)} /><Metric label="Exit" value={money(d.exit_price)} /><Metric label="Quantity" value={d.quantity} />
      <Metric label="Realized P&L" value={money(d.realized_pnl)} /><Metric label="Unrealized P&L" value={money(d.unrealized_pnl)} /><Metric label="Return" value={d.realized_return_percent === null ? "—" : `${Number(d.realized_return_percent).toFixed(2)}%`} />
      <Metric label="Holding" value={duration(d.holding_duration_seconds)} /><Metric label="MFE" value={money(d.maximum_favorable_excursion)} /><Metric label="MAE" value={money(d.maximum_adverse_excursion)} />
      <Metric label="Best price" value={money(d.best_price)} /><Metric label="Worst price" value={money(d.worst_price)} /><Metric label="Initial stop" value={money(t.initial_stop_price)} />
      <Metric label="Final stop" value={money(t.final_stop_price)} /><Metric label="Initial stop distance" value={money(d.initial_stop_distance)} /><Metric label="Final stop distance" value={money(d.final_stop_distance)} />
      <Metric label="Exit reason" value={unavailable(d.exit_reason)} /><Metric label="Protection" value={unavailable(t.protective_stop_mode)} />
    </dl>
    <div className="attribution"><p><strong>Entry:</strong> {unavailable(t.entry_strategy_id)} {t.entry_strategy_version ? `v${t.entry_strategy_version}` : ""} · signal {unavailable(t.entry_signal_id)} · decision {unavailable(t.entry_decision)}</p><p><strong>Exit:</strong> {t.exit_reason === "STRATEGY_EXIT" ? `${unavailable(t.exit_strategy_id)} ${t.exit_strategy_version ? `v${t.exit_strategy_version}` : ""}` : unavailable(t.exit_reason)}</p></div>
    <TradeChart inspection={inspection} />
    <p className="chart-note">{d.basis}. One-minute OHLC bars do not establish intrabar ordering.</p>{inspection.bars_limitation ? <p className="coverage-warning">{inspection.bars_limitation}</p> : null}
    <div className="timeline"><h4>Authoritative event timeline</h4>{inspection.events.length === 0 ? <p className="hint">No recorded trade events are available.</p> : <ol>{inspection.events.map((event, index) => <li key={`${event.timestamp}-${event.event_type}-${index}`}><time>{localTime(event.timestamp)}</time><strong>{event.event_type}</strong><span>{[event.strategy_id, event.source, event.price ? `@ ${event.price}` : null, event.quantity ? `qty ${event.quantity}` : null, event.reason].filter(Boolean).join(" · ")}</span></li>)}</ol>}</div>
    <details><summary>Data provenance and identifiers</summary><p>{unavailable(t.data_provenance)}</p><p>Entry signal: {unavailable(t.entry_signal_id)} · Exit signal: {unavailable(t.exit_signal_id)} · Research run: {unavailable(t.research_run_id)}</p></details>
  </div>;
}

function Metric({ label, value }: { label: string; value: string }) { return <div><dt>{label}</dt><dd>{value}</dd></div>; }

function TradeChart({ inspection }: { inspection: TradeInspection }) {
  const [zoom, setZoom] = useState<"session" | "trade">("session"); const width = 1000, height = 300, pad = 38;
  const bars = useMemo(() => { if (zoom === "session" || !inspection.trade.entry_timestamp) return inspection.bars;
    const start = new Date(inspection.trade.entry_timestamp).getTime() - 15 * 60_000;
    const end = (inspection.trade.exit_timestamp ? new Date(inspection.trade.exit_timestamp).getTime() : Date.now()) + 15 * 60_000;
    return inspection.bars.filter((bar) => { const at = new Date(bar.timestamp).getTime(); return at >= start && at <= end; });
  }, [inspection, zoom]);
  if (bars.length === 0) return <div className="chart-empty">Historical bars unavailable. Execution and event data remain authoritative.</div>;
  const prices = bars.flatMap((b) => [Number(b.low), Number(b.high), ...(b.vwap ? [Number(b.vwap)] : [])]);
  inspection.fills.forEach((f) => prices.push(Number(f.price))); inspection.events.filter((e) => e.event_type.includes("STOP") && e.price).forEach((e) => prices.push(Number(e.price)));
  if (inspection.trade.initial_stop_price) prices.push(Number(inspection.trade.initial_stop_price));
  if (inspection.trade.final_stop_price) prices.push(Number(inspection.trade.final_stop_price));
  const min = Math.min(...prices), max = Math.max(...prices), range = Math.max(0.01, max - min);
  const firstBar = bars[0]!; const lastBar = bars[bars.length - 1]!;
  const first = new Date(firstBar.timestamp).getTime(), last = new Date(lastBar.timestamp).getTime() + 60_000;
  const x = (time: string) => pad + (new Date(time).getTime() - first) / Math.max(1, last - first) * (width - pad * 2);
  const y = (price: number) => height - pad - (price - min) / range * (height - pad * 2);
  const candleWidth = Math.max(1, (width - pad * 2) / bars.length * 0.65);
  const vwap = bars.filter((b) => b.vwap !== null).map((b) => `${x(b.timestamp)},${y(Number(b.vwap))}`).join(" ");
  const stops = inspection.events.filter((e) => e.event_type.includes("STOP") && e.price !== null).sort((a, b) => a.timestamp.localeCompare(b.timestamp));
  const sellSignals = inspection.events.filter((e) => e.event_type === "SIGNAL_GENERATED" && e.price !== null && (e.source === "EXIT_LONG" || e.source === "SELL"));
  const stopStart = inspection.trade.entry_timestamp ?? bars[0]!.timestamp;
  const stopEnd = inspection.trade.exit_timestamp ?? bars[bars.length - 1]!.timestamp;
  const stopLevels = stops.length > 0 ? stops : inspection.trade.initial_stop_price ? [{ timestamp: stopStart, price: inspection.trade.initial_stop_price }] : [];
  return <div className="trade-chart"><div className="chart-controls"><strong>One-minute price bars</strong><div><button className={zoom === "session" ? "active" : ""} onClick={() => setZoom("session")}>Full session</button><button className={zoom === "trade" ? "active" : ""} onClick={() => setZoom("trade")}>Around trade</button></div></div>
    <svg viewBox={`0 0 ${width} ${height}`} role="img" aria-label={`${inspection.trade.symbol} trade price chart`}>
      <rect x={pad} y={pad} width={width-pad*2} height={height-pad*2} className="chart-bg" />
      {inspection.trade.entry_timestamp ? <rect x={x(inspection.trade.entry_timestamp)} y={pad} width={Math.max(2, x(inspection.trade.exit_timestamp ?? lastBar.timestamp)-x(inspection.trade.entry_timestamp))} height={height-pad*2} className="holding-band" /> : null}
      {bars.map((b) => { const cx=x(b.timestamp), open=y(Number(b.open)), close=y(Number(b.close)), high=y(Number(b.high)), low=y(Number(b.low)), up=Number(b.close)>=Number(b.open); return <g key={b.timestamp} className={up ? "candle-up" : "candle-down"}><line x1={cx} y1={high} x2={cx} y2={low}/><rect x={cx-candleWidth/2} y={Math.min(open,close)} width={candleWidth} height={Math.max(1,Math.abs(close-open))}/></g>; })}
      {vwap ? <polyline points={vwap} className="vwap-line" /> : null}
      {stopLevels.map((event, index) => { const next = stopLevels[index + 1]; const from = index === 0 ? stopStart : event.timestamp; const to = next?.timestamp ?? stopEnd; return <g key={`${event.timestamp}-${index}`} className="stop-line"><line x1={x(from)} y1={y(Number(event.price))} x2={x(to)} y2={y(Number(event.price))}/>{next ? <line x1={x(to)} y1={y(Number(event.price))} x2={x(to)} y2={y(Number(next.price))}/> : null}</g>; })}
      {sellSignals.map((event) => <g key={`signal-${event.timestamp}-${event.signal_id}`} transform={`translate(${x(event.timestamp)},${y(Number(event.price))})`} className="signal-marker"><polygon points="0,-6 6,0 0,6 -6,0"/><text y="-10">SELL signal</text></g>)}
      {inspection.fills.map((f) => <g key={f.fill_id} transform={`translate(${x(f.timestamp)},${y(Number(f.price))})`} className={f.side === "BUY" ? "buy-marker" : "sell-marker"}><circle r="6"/><text y={f.side === "BUY" ? 18 : -10}>{f.side} {f.quantity}@{f.price}</text></g>)}
      <text x={pad} y={18} className="axis-label">{max.toFixed(2)}</text><text x={pad} y={height-8} className="axis-label">{min.toFixed(2)}</text>
    </svg><div className="chart-legend"><span className="legend-vwap">VWAP</span><span className="legend-stop">Recorded stop</span><span>Shaded: holding interval</span></div></div>;
}
