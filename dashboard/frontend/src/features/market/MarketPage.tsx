/** Market Status Monitor — Phase 2C.
 * Displays the real-time five-state broad market risk classifier (NORMAL / WARNING /
 * CRASH / STABILIZING / RECOVERY), SPY/QQQ/IWM benchmark metrics, active trading
 * policy restrictions, state transition audit history, and real 1m candlestick charts.
 */

import { useMemo, useState } from "react";
import { useSelectedAccount } from "../../state/AccountContext";
import {
  useMarketOverview,
  useMarketBars,
  useMarketTransitions,
  useMarketConfig,
} from "../../state/queries";
import { EmptyState, ErrorBanner, Loading } from "../../components/ui";
import { dateTimeEt, decimal, percent, timeEt } from "../../lib/format";
import type {
  BenchmarkSymbol,
  MarketBarItem,
  MarketState,
  MarketStatusOverview,
  PermissionLevel,
} from "../../api/market";
import "./market.css";

function formatDuration(seconds: number): string {
  const s = Math.max(0, Math.floor(seconds));
  const m = Math.floor(s / 60);
  const remSec = s % 60;
  if (m === 0) return `${remSec}s`;
  if (m < 60) return `${m}m ${remSec}s`;
  const h = Math.floor(m / 60);
  const remMin = m % 60;
  return `${h}h ${remMin}m`;
}

function stateClass(state: MarketState): string {
  switch (state) {
    case "NORMAL":
      return "state-normal";
    case "WARNING":
      return "state-warning";
    case "CRASH":
      return "state-crash";
    case "STABILIZING":
      return "state-stabilizing";
    case "RECOVERY":
      return "state-recovery";
  }
}

export default function MarketPage() {
  const { selectedAccountId } = useSelectedAccount();
  const overviewQuery = useMarketOverview(selectedAccountId);
  const transitionsQuery = useMarketTransitions(selectedAccountId, 50);
  const configQuery = useMarketConfig();

  const [selectedChartSymbol, setSelectedChartSymbol] = useState<string>("SPY");
  const [chartBarLimit, setChartBarLimit] = useState<number>(120);

  const barsQuery = useMarketBars(selectedAccountId, null, chartBarLimit);

  if (overviewQuery.isLoading && !overviewQuery.data) {
    return <Loading label="Loading Market Status Monitor…" />;
  }

  if (overviewQuery.isError) {
    return (
      <ErrorBanner
        title="Market Monitor Unavailable"
        message={overviewQuery.error?.message || "Failed to load market status"}
        hint="Ensure the backend server is running and the SQLite database is reachable."
      />
    );
  }

  const overview = overviewQuery.data;
  if (!overview) {
    return <EmptyState message="No market status data available." />;
  }

  return (
    <div className="market-view">
      {/* Header bar */}
      <div className="market-header">
        <div className="market-header-title">
          <h1>Market Status Monitor</h1>
          <span className="pill pill-neutral">{overview.account_id}</span>
        </div>
        <div className="market-header-meta">
          <span>
            Heartbeat:{" "}
            <strong>
              {overview.engine_online ? (
                <span style={{ color: "var(--ok-text)" }}>ONLINE</span>
              ) : (
                <span style={{ color: "var(--bad-text)" }}>OFFLINE</span>
              )}
            </strong>
          </span>
          {overview.last_evaluation_time ? (
            <span>Evaluated: {timeEt(overview.last_evaluation_time)} ET</span>
          ) : null}
        </div>
      </div>

      {/* Offline or stale alert banner */}
      {(!overview.engine_online || overview.is_stale || overview.fail_closed) && (
        <div className="market-alert-stale">
          <div style={{ flex: 1 }}>
            <strong>Safety Lockout Active: </strong>
            {!overview.engine_online
              ? "Trading engine heartbeat is offline (>60s). Entry permissions fail closed (BLOCKED)."
              : overview.fail_closed
                ? `Monitor fail-closed lockout: ${overview.fail_closed_reason || "Data missing"}. Entries blocked.`
                : "Market observations or heartbeats are stale. New entries are restricted."}
            <div style={{ marginTop: 4, opacity: 0.9 }}>
              Safety Guarantee: All risk exits (SELL, COVER), protective stops, and emergency liquidations
              remain fully authorized and unblocked.
            </div>
          </div>
        </div>
      )}

      {/* Primary Market State Hero */}
      <MarketStateHero overview={overview} />

      {/* Composite Broad-Market Metrics */}
      <CompositeMetricsStrip overview={overview} />

      {/* Trading Implications & Safety Invariants */}
      <TradingImplicationsPanel overview={overview} />

      {/* Benchmark Reference ETFs (SPY, QQQ, IWM) */}
      <div className="benchmarks-section">
        <h3 style={{ fontSize: 13, textTransform: "uppercase", color: "var(--muted)", letterSpacing: "0.04em" }}>
          Reference Benchmark ETFs (Consensus Quorum: 2 of 3)
        </h3>
        <div className="benchmarks-grid">
          {overview.benchmarks.map((b) => (
            <BenchmarkCard key={b.symbol} benchmark={b} />
          ))}
        </div>
      </div>

      {/* Interactive Candlestick / Price Chart */}
      <MarketCandleChart
        barsData={barsQuery.data}
        isLoading={barsQuery.isLoading}
        selectedSymbol={selectedChartSymbol}
        onSelectSymbol={setSelectedChartSymbol}
        barLimit={chartBarLimit}
        onSelectLimit={setChartBarLimit}
      />

      {/* State Transition Audit History */}
      <TransitionAuditTable
        transitions={transitionsQuery.data?.transitions || []}
        isLoading={transitionsQuery.isLoading}
      />

      {/* Technical Thresholds & Configuration */}
      <ConfigReference config={configQuery.data} />
    </div>
  );
}

// ---------------------------------------------------------------------------
// 1. Hero Panel
// ---------------------------------------------------------------------------

function MarketStateHero({ overview }: { overview: MarketStatusOverview }) {
  const stClass = stateClass(overview.market_state);

  return (
    <div className={`market-hero ${stClass}`}>
      <div className="market-hero-main">
        <div className="market-state-display">
          <div className={`market-state-pill-large state-pill-${overview.market_state}`}>
            <span className="state-dot" aria-hidden="true" />
            <span>{overview.market_state}</span>
          </div>
          {overview.previous_state && (
            <span style={{ color: "var(--muted)", fontSize: 13 }}>
              (was {overview.previous_state})
            </span>
          )}
        </div>

        <div className="market-hero-kpis">
          <div className="market-hero-kpi-item">
            <span className="market-hero-kpi-label">State Duration</span>
            <span className="market-hero-kpi-val">
              {formatDuration(overview.duration_seconds)}
              <small style={{ fontSize: 11, color: "var(--muted)", marginLeft: 6 }}>
                ({overview.bars_in_state} bars)
              </small>
            </span>
          </div>
          <div className="market-hero-kpi-item">
            <span className="market-hero-kpi-label">State Since</span>
            <span className="market-hero-kpi-val">{timeEt(overview.state_since)} ET</span>
          </div>
          <div className="market-hero-kpi-item">
            <span className="market-hero-kpi-label">Benchmark Freshness</span>
            <span
              className="market-hero-kpi-val"
              style={{ color: overview.is_stale ? "var(--warn-text)" : "var(--ok-text)" }}
            >
              {overview.is_stale ? "STALE" : "FRESH"}
            </span>
          </div>
        </div>
      </div>

      <div className="market-hero-details">
        <div className="hero-detail-block">
          <strong>State Trigger & Root Cause</strong>
          <p>{overview.reason || "Within normal baseline tolerances."}</p>
        </div>
        <div className="hero-detail-block">
          <strong>Operational Recovery Requirements</strong>
          <p>{overview.recovery_requirements}</p>
        </div>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// 2. Composite Metrics Strip
// ---------------------------------------------------------------------------

function CompositeMetricsStrip({ overview }: { overview: MarketStatusOverview }) {
  const comp = overview.composite_metrics;

  const ddColor =
    comp.composite_drawdown_percent === null
      ? "var(--text)"
      : comp.composite_drawdown_percent <= -3.5
        ? "var(--bad-text)"
        : comp.composite_drawdown_percent <= -1.5
          ? "var(--warn-text)"
          : "var(--ok-text)";

  return (
    <div className="composite-strip">
      <div className="composite-stat">
        <span className="composite-label">Composite Drawdown</span>
        <span className="composite-val" style={{ color: ddColor }}>
          {comp.composite_drawdown_percent !== null
            ? percent(comp.composite_drawdown_percent, 2)
            : "—"}
        </span>
      </div>
      <div className="composite-stat">
        <span className="composite-label">Worst Drawdown</span>
        <span className="composite-val">
          {comp.worst_drawdown_percent !== null
            ? percent(comp.worst_drawdown_percent, 2)
            : "—"}
        </span>
      </div>
      <div className="composite-stat">
        <span className="composite-label">Realized Volatility</span>
        <span className="composite-val">
          {comp.realized_volatility_bps !== null
            ? `${decimal(comp.realized_volatility_bps, 0)} bps`
            : "—"}
        </span>
      </div>
      <div className="composite-stat">
        <span className="composite-label">Momentum vs 20m SMA</span>
        <span className="composite-val">
          {comp.momentum_bps !== null ? `${decimal(comp.momentum_bps, 0)} bps` : "—"}
        </span>
      </div>
      <div className="composite-stat">
        <span className="composite-label">Breadth Weakness</span>
        <span className="composite-val">
          {comp.breadth_weakness_ratio !== null
            ? percent(comp.breadth_weakness_ratio * 100, 0)
            : "—"}
        </span>
      </div>
      <div className="composite-stat">
        <span className="composite-label">Episode Trough</span>
        <span className="composite-val">
          {comp.trough_drawdown_percent !== null
            ? percent(comp.trough_drawdown_percent, 2)
            : "—"}
        </span>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// 3. Trading Implications & Safety Invariants
// ---------------------------------------------------------------------------

function PermissionBadge({ level }: { level: PermissionLevel }) {
  return <span className={`perm-pill perm-${level}`}>{level}</span>;
}

function TradingImplicationsPanel({ overview }: { overview: MarketStatusOverview }) {
  const auth = overview.authorization;

  return (
    <div className="market-implications-grid">
      <div className="implications-card">
        <h3>
          <span>Strategy Execution Authorization</span>
          <span style={{ fontSize: 11, fontWeight: 500, color: "var(--muted)" }}>
            Engine Policy
          </span>
        </h3>
        <div className="permissions-row">
          <div className="permission-entry">
            <span className="permission-entry-title">BUY Entries (Longs)</span>
            <PermissionBadge level={auth.buy_permission} />
            <span style={{ fontSize: 11, color: "var(--muted)", marginTop: 4 }}>
              {auth.buy_allowed
                ? auth.buy_permission === "RESTRICTED"
                  ? "Reduced sizing permitted"
                  : "Normal capital allocation"
                : "New long entries prohibited"}
            </span>
          </div>

          <div className="permission-entry">
            <span className="permission-entry-title">SHORT Entries (Shorts)</span>
            <PermissionBadge level={auth.short_permission} />
            <span style={{ fontSize: 11, color: "var(--muted)", marginTop: 4 }}>
              {auth.short_allowed
                ? auth.short_permission === "RESTRICTED"
                  ? "Reduced short sizing"
                  : "Authorized under crash policy"
                : "New short entries prohibited"}
            </span>
          </div>
        </div>

        {auth.reasons.length > 0 && (
          <div style={{ fontSize: 11.5, color: "var(--muted)", marginTop: 4 }}>
            Policy notes: {auth.reasons.join(", ")}
          </div>
        )}
      </div>

      <div className="implications-card">
        <h3>
          <span>Core Safety Invariants</span>
          <span style={{ fontSize: 11, fontWeight: 500, color: "var(--ok-text)" }}>
            Guaranteed Active
          </span>
        </h3>
        <div className="invariants-list">
          <div className="invariant-item">
            <span>SELL Exits (Long Position Closes)</span>
            <span className="invariant-badge">AUTHORIZED</span>
          </div>
          <div className="invariant-item">
            <span>COVER Exits (Short Position Closes)</span>
            <span className="invariant-badge">AUTHORIZED</span>
          </div>
          <div className="invariant-item">
            <span>Protective & Trailing Stops</span>
            <span className="invariant-badge">ACTIVE & UNBLOCKED</span>
          </div>
          <div className="invariant-item">
            <span>Emergency Liquidation</span>
            <span className="invariant-badge">AUTHORIZED</span>
          </div>
        </div>
        <div className="invariant-footer-note">
          Safety Rule: Protective stops and exit executions are never blocked or suspended under
          any market condition or data outage.
        </div>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// 4. Benchmark Reference Cards
// ---------------------------------------------------------------------------

function BenchmarkCard({ benchmark }: { benchmark: BenchmarkSymbol }) {
  const dd = benchmark.drawdown_percent ?? 0.0;
  const isCrash = benchmark.crash_breached;
  const isWarning = benchmark.warning_breached;

  // Percentage bar width normalized to -5% scale
  const barWidth = Math.min(100, Math.max(0, (Math.abs(dd) / 5.0) * 100));
  const fillClass = isCrash ? "fill-crash" : isWarning ? "fill-warning" : "fill-normal";

  return (
    <div className={`benchmark-card ${isCrash || isWarning ? "has-breach" : ""}`}>
      <div className="benchmark-card-head">
        <div>
          <span className="benchmark-symbol">{benchmark.symbol}</span>
          {isCrash ? (
            <span className="pill pill-red" style={{ marginLeft: 8, fontSize: 10 }}>
              CRASH BREACH
            </span>
          ) : isWarning ? (
            <span className="pill pill-yellow" style={{ marginLeft: 8, fontSize: 10 }}>
              WARNING BREACH
            </span>
          ) : (
            <span className="pill pill-green" style={{ marginLeft: 8, fontSize: 10 }}>
              NORMAL
            </span>
          )}
        </div>
        <span className="benchmark-price">
          {benchmark.latest_price !== null ? `$${decimal(benchmark.latest_price, 2)}` : "—"}
        </span>
      </div>

      <div className="benchmark-drawdown-bar-container">
        <div className="benchmark-drawdown-label-row">
          <span style={{ color: "var(--muted)" }}>Drawdown from Peak</span>
          <strong
            style={{
              fontFamily: "var(--mono)",
              color: isCrash ? "var(--bad-text)" : isWarning ? "var(--warn-text)" : "var(--text)",
            }}
          >
            {benchmark.drawdown_percent !== null ? percent(benchmark.drawdown_percent, 2) : "—"}
          </strong>
        </div>
        <div className="drawdown-track">
          <div className={`drawdown-fill ${fillClass}`} style={{ width: `${barWidth}%` }} />
        </div>
      </div>

      <div className="benchmark-stats-grid">
        <div className="benchmark-stat-item">
          <span className="benchmark-stat-label">390m Peak High</span>
          <span className="benchmark-stat-val">
            {benchmark.peak_price !== null ? `$${decimal(benchmark.peak_price, 2)}` : "—"}
          </span>
        </div>
        <div className="benchmark-stat-item">
          <span className="benchmark-stat-label">Volatility (20m)</span>
          <span className="benchmark-stat-val">
            {benchmark.realized_volatility_bps !== null
              ? `${decimal(benchmark.realized_volatility_bps, 0)} bps`
              : "—"}
          </span>
        </div>
        <div className="benchmark-stat-item">
          <span className="benchmark-stat-label">Momentum (20m SMA)</span>
          <span className="benchmark-stat-val">
            {benchmark.momentum_bps !== null
              ? `${decimal(benchmark.momentum_bps, 0)} bps`
              : "—"}
          </span>
        </div>
        <div className="benchmark-stat-item">
          <span className="benchmark-stat-label">Last 1m Bar</span>
          <span className="benchmark-stat-val">
            {benchmark.last_bar_timestamp ? timeEt(benchmark.last_bar_timestamp) : "—"}
          </span>
        </div>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// 5. Candlestick & Price Chart
// ---------------------------------------------------------------------------

interface ChartProps {
  barsData: any;
  isLoading: boolean;
  selectedSymbol: string;
  onSelectSymbol: (sym: string) => void;
  barLimit: number;
  onSelectLimit: (lim: number) => void;
}

const CHART_W = 1000;
const CHART_H = 280;
const PAD_LEFT = 60;
const PAD_RIGHT = 30;
const PAD_TOP = 20;
const PAD_BOT = 30;

function MarketCandleChart({
  barsData,
  isLoading,
  selectedSymbol,
  onSelectSymbol,
  barLimit,
  onSelectLimit,
}: ChartProps) {
  const symbols = ["SPY", "QQQ", "IWM"];

  const currentBars: MarketBarItem[] = useMemo(() => {
    if (!barsData?.symbols) return [];
    const symMatch = barsData.symbols.find((s: any) => s.symbol === selectedSymbol);
    return symMatch?.bars || [];
  }, [barsData, selectedSymbol]);

  // Compute coordinate geometry
  const chartGeometry = useMemo(() => {
    if (currentBars.length === 0) return null;

    let minPrice = Infinity;
    let maxPrice = -Infinity;
    let maxVolume = 0;

    for (const b of currentBars) {
      if (b.low < minPrice) minPrice = b.low;
      if (b.high > maxPrice) maxPrice = b.high;
      if (b.volume > maxVolume) maxVolume = b.volume;
    }

    if (minPrice === Infinity || maxPrice === -Infinity) return null;

    // Buffer range by 5%
    const pRange = Math.max(0.01, maxPrice - minPrice);
    minPrice -= pRange * 0.05;
    maxPrice += pRange * 0.05;
    const finalRange = maxPrice - minPrice;

    const plotW = CHART_W - PAD_LEFT - PAD_RIGHT;
    const plotH = CHART_H - PAD_TOP - PAD_BOT;
    const candleSlotW = plotW / currentBars.length;
    const candleBodyW = Math.max(2, Math.min(10, candleSlotW * 0.7));

    const yPrice = (p: number) => PAD_TOP + plotH - ((p - minPrice) / finalRange) * plotH;
    const xSlot = (idx: number) => PAD_LEFT + idx * candleSlotW + candleSlotW / 2;

    return {
      minPrice,
      maxPrice,
      maxVolume,
      plotW,
      plotH,
      candleBodyW,
      yPrice,
      xSlot,
    };
  }, [currentBars]);

  return (
    <div className="market-chart-card">
      <div className="market-chart-toolbar">
        <div style={{ display: "flex", alignItems: "center", gap: 12 }}>
          <strong style={{ fontSize: 13, textTransform: "uppercase", letterSpacing: "0.04em", color: "var(--muted)" }}>
            Benchmark Candlesticks (1m Bars)
          </strong>
          <div className="chart-symbol-buttons">
            {symbols.map((s) => (
              <button
                key={s}
                type="button"
                className={`chart-btn ${selectedSymbol === s ? "active" : ""}`}
                onClick={() => onSelectSymbol(s)}
              >
                {s}
              </button>
            ))}
          </div>
        </div>

        <div className="chart-bars-buttons">
          {[60, 120, 240].map((lim) => (
            <button
              key={lim}
              type="button"
              className={`chart-btn ${barLimit === lim ? "active" : ""}`}
              onClick={() => onSelectLimit(lim)}
            >
              {lim} bars
            </button>
          ))}
        </div>
      </div>

      <div className="market-svg-container">
        {isLoading ? (
          <div style={{ display: "flex", alignItems: "center", justifyContent: "center", height: "100%", color: "var(--muted)" }}>
            Loading candlestick data…
          </div>
        ) : !chartGeometry || currentBars.length === 0 ? (
          <div style={{ display: "flex", alignItems: "center", justifyContent: "center", height: "100%", color: "var(--muted)" }}>
            No recorded 1m market bars found for {selectedSymbol} in the current session.
          </div>
        ) : (
          <svg viewBox={`0 0 ${CHART_W} ${CHART_H}`} role="img" aria-label={`${selectedSymbol} Candlestick Chart`}>
            {/* Background gridlines */}
            {[0, 0.25, 0.5, 0.75, 1.0].map((ratio) => {
              const y = PAD_TOP + ratio * chartGeometry.plotH;
              const priceAtY = chartGeometry.maxPrice - ratio * (chartGeometry.maxPrice - chartGeometry.minPrice);
              return (
                <g key={ratio}>
                  <line
                    x1={PAD_LEFT}
                    y1={y}
                    x2={CHART_W - PAD_RIGHT}
                    y2={y}
                    stroke="var(--border)"
                    strokeDasharray="3 3"
                    opacity={0.4}
                  />
                  <text
                    x={PAD_LEFT - 8}
                    y={y + 4}
                    fill="var(--muted)"
                    fontSize="10"
                    textAnchor="end"
                    fontFamily="var(--mono)"
                  >
                    ${priceAtY.toFixed(2)}
                  </text>
                </g>
              );
            })}

            {/* Candlesticks */}
            {currentBars.map((b, idx) => {
              const cx = chartGeometry.xSlot(idx);
              const yOpen = chartGeometry.yPrice(b.open);
              const yClose = chartGeometry.yPrice(b.close);
              const yHigh = chartGeometry.yPrice(b.high);
              const yLow = chartGeometry.yPrice(b.low);

              const isUp = b.close >= b.open;
              const color = isUp ? "var(--ok-text)" : "var(--bad-text)";
              const bodyTop = Math.min(yOpen, yClose);
              const bodyHeight = Math.max(1.5, Math.abs(yClose - yOpen));

              return (
                <g key={idx}>
                  {/* Wick */}
                  <line x1={cx} y1={yHigh} x2={cx} y2={yLow} stroke={color} strokeWidth={1} />
                  {/* Body */}
                  <rect
                    x={cx - chartGeometry.candleBodyW / 2}
                    y={bodyTop}
                    width={chartGeometry.candleBodyW}
                    height={bodyHeight}
                    fill={color}
                  />
                </g>
              );
            })}

            {currentBars.length > 0 && currentBars[0] && currentBars[currentBars.length - 1] && (
              <>
                <text
                  x={PAD_LEFT}
                  y={CHART_H - 10}
                  fill="var(--muted)"
                  fontSize="10"
                  textAnchor="start"
                  fontFamily="var(--mono)"
                >
                  {timeEt(currentBars[0]?.timestamp)}
                </text>
                <text
                  x={PAD_LEFT + chartGeometry.plotW / 2}
                  y={CHART_H - 10}
                  fill="var(--muted)"
                  fontSize="10"
                  textAnchor="middle"
                  fontFamily="var(--mono)"
                >
                  {timeEt(currentBars[Math.floor(currentBars.length / 2)]?.timestamp)}
                </text>
                <text
                  x={CHART_W - PAD_RIGHT}
                  y={CHART_H - 10}
                  fill="var(--muted)"
                  fontSize="10"
                  textAnchor="end"
                  fontFamily="var(--mono)"
                >
                  {timeEt(currentBars[currentBars.length - 1]?.timestamp)}
                </text>
              </>
            )}
          </svg>
        )}
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// 6. Transition Audit Table
// ---------------------------------------------------------------------------

function TransitionAuditTable({
  transitions,
  isLoading,
}: {
  transitions: any[];
  isLoading: boolean;
}) {
  return (
    <div className="transitions-card">
      <h3 style={{ fontSize: 13, textTransform: "uppercase", letterSpacing: "0.04em", color: "var(--muted)" }}>
        State Transition History (Audit Trail)
      </h3>

      {isLoading ? (
        <div style={{ padding: 16, color: "var(--muted)" }}>Loading transitions…</div>
      ) : transitions.length === 0 ? (
        <div style={{ padding: 16, color: "var(--muted)", fontStyle: "italic" }}>
          No state transitions recorded in this session.
        </div>
      ) : (
        <div style={{ overflowX: "auto" }}>
          <table className="transitions-table">
            <thead>
              <tr>
                <th>Transition Time (ET)</th>
                <th>Transition</th>
                <th>Trigger Reason</th>
                <th>Metrics at Transition</th>
              </tr>
            </thead>
            <tbody>
              {transitions.map((t, idx) => (
                <tr key={idx}>
                  <td style={{ fontFamily: "var(--mono)", whiteSpace: "nowrap" }}>
                    {dateTimeEt(t.timestamp)}
                  </td>
                  <td style={{ whiteSpace: "nowrap" }}>
                    <span className={`state-badge-micro state-pill-${t.from_state}`}>
                      {t.from_state}
                    </span>
                    <span className="transition-arrow">&rarr;</span>
                    <span className={`state-badge-micro state-pill-${t.to_state}`}>
                      {t.to_state}
                    </span>
                  </td>
                  <td>{t.reason}</td>
                  <td style={{ fontFamily: "var(--mono)", fontSize: 11.5, color: "var(--muted)" }}>
                    {t.metrics_summary?.composite_drawdown_percent !== undefined
                      ? `Drawdown: ${percent(t.metrics_summary.composite_drawdown_percent, 2)}`
                      : t.metrics_summary?.composite_drawdown_pct !== undefined
                        ? `Drawdown: ${percent(t.metrics_summary.composite_drawdown_pct, 2)}`
                        : "—"}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// 7. Config & Rules Disclosure
// ---------------------------------------------------------------------------

function ConfigReference({ config }: { config: any }) {
  if (!config) return null;

  return (
    <details className="config-disclosure">
      <summary>Technical Parameters & Transition Thresholds (Engine Config)</summary>
      <div className="config-grid">
        <div className="config-param-item">
          <span className="config-param-name">Reference Universe</span>
          <span className="config-param-val">
            {(config.reference_symbols || ["SPY", "QQQ", "IWM"]).join(", ")}
          </span>
        </div>
        <div className="config-param-item">
          <span className="config-param-name">Consensus Quorum</span>
          <span className="config-param-val">
            {config.min_required_symbols} of {config.reference_symbols?.length || 3} members
          </span>
        </div>
        <div className="config-param-item">
          <span className="config-param-name">Warning Drawdown</span>
          <span className="config-param-val" style={{ color: "var(--warn-text)" }}>
            {percent(config.warning_drawdown_percent, 1)}
          </span>
        </div>
        <div className="config-param-item">
          <span className="config-param-name">Crash Drawdown</span>
          <span className="config-param-val" style={{ color: "var(--bad-text)" }}>
            {percent(config.crash_drawdown_percent, 1)}
          </span>
        </div>
        <div className="config-param-item">
          <span className="config-param-name">Flash Crash Acute</span>
          <span className="config-param-val" style={{ color: "var(--bad-text)" }}>
            {percent(config.flash_crash_drawdown_percent, 1)}
          </span>
        </div>
        <div className="config-param-item">
          <span className="config-param-name">Warning Cleared Level</span>
          <span className="config-param-val" style={{ color: "var(--ok-text)" }}>
            {percent(config.warning_cleared_percent, 1)}
          </span>
        </div>
        <div className="config-param-item">
          <span className="config-param-name">Confirmation Bars (Warn)</span>
          <span className="config-param-val">{config.warning_confirmation_bars} bars</span>
        </div>
        <div className="config-param-item">
          <span className="config-param-name">Confirmation Bars (Crash)</span>
          <span className="config-param-val">{config.crash_confirmation_bars} bars</span>
        </div>
        <div className="config-param-item">
          <span className="config-param-name">Stabilizing Min In Crash</span>
          <span className="config-param-val">{config.stabilizing_min_bars_in_crash} bars</span>
        </div>
        <div className="config-param-item">
          <span className="config-param-name">Recovery Confirmation</span>
          <span className="config-param-val">{config.recovery_confirmation_bars} bars</span>
        </div>
        <div className="config-param-item">
          <span className="config-param-name">Peak Lookback Window</span>
          <span className="config-param-val">{config.peak_lookback_bars} bars (390m)</span>
        </div>
        <div className="config-param-item">
          <span className="config-param-name">Observation Staleness Limit</span>
          <span className="config-param-val">{config.max_observation_age_seconds}s</span>
        </div>
      </div>
    </details>
  );
}
