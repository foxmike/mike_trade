/** Interim price chart for trade inspection, rendering the API's real 1-minute
 * OHLC bars (existing functionality, ported). The dedicated Trade Explorer
 * workstream replaces this with interactive candlestick charting; do not
 * render synthetic bars — when bars are unavailable an explicit empty state
 * is shown instead. */

import { useMemo, useState } from "react";
import type { TradeInspection } from "../../api/client";

const WIDTH = 1000;
const HEIGHT = 300;
const PAD = 38;

export default function TradeChart({ inspection }: { inspection: TradeInspection }) {
  const [zoom, setZoom] = useState<"session" | "trade">("session");

  const bars = useMemo(() => {
    if (zoom === "session" || inspection.trade.entry_timestamp === null) return inspection.bars;
    const start = new Date(inspection.trade.entry_timestamp).getTime() - 15 * 60_000;
    const end =
      (inspection.trade.exit_timestamp ? new Date(inspection.trade.exit_timestamp).getTime() : Date.now()) +
      15 * 60_000;
    return inspection.bars.filter((bar) => {
      const at = new Date(bar.timestamp).getTime();
      return at >= start && at <= end;
    });
  }, [inspection, zoom]);

  if (bars.length === 0) {
    return (
      <div className="chart-empty">
        Historical bars unavailable for this trade. Execution and event data remain authoritative.
        {inspection.bars_limitation !== null ? <p className="hint">{inspection.bars_limitation}</p> : null}
      </div>
    );
  }

  const prices: number[] = [];
  for (const bar of bars) {
    prices.push(Number(bar.low), Number(bar.high));
    if (bar.vwap !== null) prices.push(Number(bar.vwap));
  }
  for (const fill of inspection.fills) prices.push(Number(fill.price));
  for (const event of inspection.events) {
    if (event.event_type.includes("STOP") && event.price !== null) prices.push(Number(event.price));
  }
  if (inspection.trade.initial_stop_price !== null) prices.push(Number(inspection.trade.initial_stop_price));
  if (inspection.trade.final_stop_price !== null) prices.push(Number(inspection.trade.final_stop_price));

  const min = Math.min(...prices);
  const max = Math.max(...prices);
  const range = Math.max(0.01, max - min);
  const firstBar = bars[0];
  const lastBar = bars[bars.length - 1];
  if (firstBar === undefined || lastBar === undefined) return null;
  const first = new Date(firstBar.timestamp).getTime();
  const last = new Date(lastBar.timestamp).getTime() + 60_000;

  const x = (time: string) => PAD + ((new Date(time).getTime() - first) / Math.max(1, last - first)) * (WIDTH - PAD * 2);
  const y = (price: number) => HEIGHT - PAD - ((price - min) / range) * (HEIGHT - PAD * 2);
  const candleWidth = Math.max(1, ((WIDTH - PAD * 2) / bars.length) * 0.65);

  const vwap = bars
    .filter((bar) => bar.vwap !== null)
    .map((bar) => `${x(bar.timestamp)},${y(Number(bar.vwap))}`)
    .join(" ");

  const stops = inspection.events
    .filter((event) => event.event_type.includes("STOP") && event.price !== null)
    .sort((a, b) => a.timestamp.localeCompare(b.timestamp));
  const stopStart = inspection.trade.entry_timestamp ?? firstBar.timestamp;
  const stopEnd = inspection.trade.exit_timestamp ?? lastBar.timestamp;
  const stopLevels =
    stops.length > 0
      ? stops
      : inspection.trade.initial_stop_price !== null
        ? [{ timestamp: stopStart, price: inspection.trade.initial_stop_price }]
        : [];

  const sellSignals = inspection.events.filter(
    (event) => event.event_type === "SIGNAL_GENERATED" && event.price !== null && (event.source === "EXIT_LONG" || event.source === "SELL"),
  );

  return (
    <div className="trade-chart">
      <div className="chart-controls">
        <strong>One-minute price bars</strong>
        <div>
          <button type="button" className={zoom === "session" ? "active" : ""} onClick={() => setZoom("session")}>
            Full session
          </button>
          <button type="button" className={zoom === "trade" ? "active" : ""} onClick={() => setZoom("trade")}>
            Around trade
          </button>
        </div>
      </div>
      <svg viewBox={`0 0 ${WIDTH} ${HEIGHT}`} role="img" aria-label={`${inspection.trade.symbol} trade price chart`}>
        <rect x={PAD} y={PAD} width={WIDTH - PAD * 2} height={HEIGHT - PAD * 2} className="chart-bg" />
        {inspection.trade.entry_timestamp !== null ? (
          <rect
            x={x(inspection.trade.entry_timestamp)}
            y={PAD}
            width={Math.max(
              2,
              x(inspection.trade.exit_timestamp ?? lastBar.timestamp) - x(inspection.trade.entry_timestamp),
            )}
            height={HEIGHT - PAD * 2}
            className="holding-band"
          />
        ) : null}
        {bars.map((bar) => {
          const cx = x(bar.timestamp);
          const open = y(Number(bar.open));
          const close = y(Number(bar.close));
          const high = y(Number(bar.high));
          const low = y(Number(bar.low));
          const up = Number(bar.close) >= Number(bar.open);
          return (
            <g key={bar.timestamp} className={up ? "candle-up" : "candle-down"}>
              <line x1={cx} y1={high} x2={cx} y2={low} />
              <rect x={cx - candleWidth / 2} y={Math.min(open, close)} width={candleWidth} height={Math.max(1, Math.abs(close - open))} />
            </g>
          );
        })}
        {vwap !== "" ? <polyline points={vwap} className="vwap-line" /> : null}
        {stopLevels.map((event, index) => {
          const next = stopLevels[index + 1];
          const from = index === 0 ? stopStart : event.timestamp;
          const to = next?.timestamp ?? stopEnd;
          return (
            <g key={`${event.timestamp}-${index}`} className="stop-line">
              <line x1={x(from)} y1={y(Number(event.price))} x2={x(to)} y2={y(Number(event.price))} />
              {next !== undefined ? (
                <line x1={x(to)} y1={y(Number(event.price))} x2={x(to)} y2={y(Number(next.price))} />
              ) : null}
            </g>
          );
        })}
        {sellSignals.map((event) => (
          <g
            key={`signal-${event.timestamp}-${event.signal_id}`}
            transform={`translate(${x(event.timestamp)},${y(Number(event.price))})`}
            className="signal-marker"
          >
            <polygon points="0,-6 6,0 0,6 -6,0" />
            <text y="-10">SELL signal</text>
          </g>
        ))}
        {inspection.fills.map((fill) => (
          <g
            key={fill.fill_id}
            transform={`translate(${x(fill.timestamp)},${y(Number(fill.price))})`}
            className={fill.side === "BUY" ? "buy-marker" : "sell-marker"}
          >
            <circle r="6" />
            <text y={fill.side === "BUY" ? 18 : -10}>
              {fill.side} {fill.quantity}@{fill.price}
            </text>
          </g>
        ))}
        <text x={PAD} y={18} className="axis-label">
          {max.toFixed(2)}
        </text>
        <text x={PAD} y={HEIGHT - 8} className="axis-label">
          {min.toFixed(2)}
        </text>
      </svg>
      <div className="chart-legend">
        <span className="legend-vwap">VWAP</span>
        <span className="legend-stop">Recorded stop</span>
        <span>Shaded: holding interval</span>
      </div>
    </div>
  );
}
