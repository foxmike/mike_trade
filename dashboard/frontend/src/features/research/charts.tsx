/** Dependency-free SVG charts for the Research Lab. All inputs are series that
 * the API reconstructed from recorded results; the components only draw. */

import { useEffect, useMemo, useRef, useState } from "react";
import type { ReactNode } from "react";

const MARGIN = { top: 10, right: 14, bottom: 24, left: 58 };

export interface ChartSeries {
  id: string;
  label: string;
  color: string;
  points: Array<{ x: number; y: number }>;
  /** Hold the previous value until the next point (cumulative trade P&L). */
  step?: boolean;
  dashed?: boolean;
}

function useWidth(): [React.RefObject<HTMLDivElement | null>, number] {
  const ref = useRef<HTMLDivElement | null>(null);
  const [width, setWidth] = useState(720);
  useEffect(() => {
    const node = ref.current;
    if (node === null || typeof ResizeObserver === "undefined") return;
    const observer = new ResizeObserver((entries) => {
      const next = entries[0]?.contentRect.width;
      if (next !== undefined && next > 80) setWidth(Math.floor(next));
    });
    observer.observe(node);
    setWidth(Math.max(240, Math.floor(node.getBoundingClientRect().width)));
    return () => observer.disconnect();
  }, []);
  return [ref, width];
}

function niceTicks(min: number, max: number, count = 5): number[] {
  if (!Number.isFinite(min) || !Number.isFinite(max)) return [];
  if (min === max) return [min];
  const span = max - min;
  const rough = span / Math.max(1, count - 1);
  const magnitude = 10 ** Math.floor(Math.log10(rough));
  const residual = rough / magnitude;
  const step = (residual >= 5 ? 10 : residual >= 2 ? 5 : residual >= 1 ? 2 : 1) * magnitude;
  const ticks: number[] = [];
  for (let value = Math.ceil(min / step) * step; value <= max + step * 1e-9; value += step) {
    ticks.push(Number(value.toFixed(12)));
  }
  return ticks;
}

function nearestIndex(points: Array<{ x: number }>, x: number): number {
  let lo = 0;
  let hi = points.length - 1;
  while (lo < hi) {
    const mid = (lo + hi) >> 1;
    if ((points[mid]?.x ?? 0) < x) lo = mid + 1;
    else hi = mid;
  }
  if (lo > 0) {
    const prev = points[lo - 1]?.x ?? 0;
    const here = points[lo]?.x ?? 0;
    if (Math.abs(prev - x) <= Math.abs(here - x)) return lo - 1;
  }
  return lo;
}

export function ChartFrame({
  title,
  caption,
  children,
  footnote,
}: {
  title: string;
  caption?: ReactNode;
  children: ReactNode;
  footnote?: ReactNode;
}) {
  return (
    <figure className="rl-chart">
      <figcaption>
        <strong>{title}</strong>
        {caption !== undefined ? <span className="rl-chart-caption">{caption}</span> : null}
      </figcaption>
      {children}
      {footnote !== undefined ? <p className="rl-chart-foot">{footnote}</p> : null}
    </figure>
  );
}

export function Legend({ series }: { series: Array<{ id: string; label: string; color: string; dashed?: boolean }> }) {
  return (
    <ul className="rl-legend" aria-label="Chart legend">
      {series.map((item) => (
        <li key={item.id}>
          <span className="rl-swatch" style={{ background: item.color }} aria-hidden="true" />
          {item.label}
        </li>
      ))}
    </ul>
  );
}

export function LineChart({
  series,
  height = 220,
  xFormat,
  yFormat,
  baseline,
  area = false,
  ariaLabel,
  emptyMessage = "No recorded data for this series.",
}: {
  series: ChartSeries[];
  height?: number;
  /** x is epoch milliseconds for time axes; supply a custom formatter otherwise. */
  xFormat: (x: number) => string;
  yFormat: (y: number) => string;
  baseline?: number;
  /** Fill between each line and the baseline (drawdown "underwater" view). */
  area?: boolean;
  ariaLabel: string;
  emptyMessage?: string;
}) {
  const [ref, width] = useWidth();
  const [hover, setHover] = useState<number | null>(null);
  const live = series.filter((item) => item.points.length > 0);

  const geometry = useMemo(() => {
    if (live.length === 0) return null;
    let xMin = Infinity;
    let xMax = -Infinity;
    let yMin = Infinity;
    let yMax = -Infinity;
    for (const item of live) {
      for (const point of item.points) {
        if (point.x < xMin) xMin = point.x;
        if (point.x > xMax) xMax = point.x;
        if (point.y < yMin) yMin = point.y;
        if (point.y > yMax) yMax = point.y;
      }
    }
    if (baseline !== undefined) {
      yMin = Math.min(yMin, baseline);
      yMax = Math.max(yMax, baseline);
    }
    if (yMin === yMax) {
      yMin -= 1;
      yMax += 1;
    }
    const pad = (yMax - yMin) * 0.06;
    return { xMin, xMax: xMax === xMin ? xMin + 1 : xMax, yMin: yMin - pad, yMax: yMax + pad };
  }, [live, baseline]);

  if (geometry === null) return <div className="chart-empty">{emptyMessage}</div>;

  const innerW = Math.max(40, width - MARGIN.left - MARGIN.right);
  const innerH = height - MARGIN.top - MARGIN.bottom;
  const sx = (x: number) => MARGIN.left + ((x - geometry.xMin) / (geometry.xMax - geometry.xMin)) * innerW;
  const sy = (y: number) => MARGIN.top + (1 - (y - geometry.yMin) / (geometry.yMax - geometry.yMin)) * innerH;

  const yTicks = niceTicks(geometry.yMin, geometry.yMax, Math.max(3, Math.round(height / 55)));
  const xTickCount = Math.max(2, Math.min(6, Math.floor(innerW / 110)));
  const xTicks = Array.from(
    { length: xTickCount },
    (_, i) => geometry.xMin + ((geometry.xMax - geometry.xMin) * i) / (xTickCount - 1),
  );
  const zeroY = baseline !== undefined ? sy(baseline) : sy(Math.max(geometry.yMin, Math.min(0, geometry.yMax)));

  const pathFor = (item: ChartSeries): string => {
    let d = "";
    item.points.forEach((point, index) => {
      const px = sx(point.x).toFixed(1);
      const py = sy(point.y).toFixed(1);
      if (index === 0) d += `M${px},${py}`;
      else if (item.step) d += `H${px}V${py}`;
      else d += `L${px},${py}`;
    });
    return d;
  };

  const reference = live[0];
  const hoverPoint = hover !== null && reference !== undefined ? reference.points[hover] : undefined;
  const hoverX = hoverPoint !== undefined ? sx(hoverPoint.x) : null;

  const onMove = (event: React.PointerEvent<SVGRectElement>) => {
    if (reference === undefined) return;
    const rect = event.currentTarget.getBoundingClientRect();
    const ratio = (event.clientX - rect.left) / rect.width;
    const x = geometry.xMin + ratio * (geometry.xMax - geometry.xMin);
    setHover(nearestIndex(reference.points, x));
  };

  const tooltipRows =
    hoverPoint === undefined
      ? []
      : live.map((item) => {
          const idx = nearestIndex(item.points, hoverPoint.x);
          return { item, point: item.points[idx] };
        });

  return (
    <div className="rl-chart-body" ref={ref}>
      <svg
        width={width}
        height={height}
        role="img"
        aria-label={ariaLabel}
        onPointerLeave={() => setHover(null)}
      >
        <rect x={MARGIN.left} y={MARGIN.top} width={innerW} height={innerH} className="rl-plot-bg" />
        {yTicks.map((tick) => (
          <g key={`y${String(tick)}`}>
            <line x1={MARGIN.left} x2={MARGIN.left + innerW} y1={sy(tick)} y2={sy(tick)} className="rl-grid" />
            <text x={MARGIN.left - 6} y={sy(tick) + 3} textAnchor="end" className="axis-label">
              {yFormat(tick)}
            </text>
          </g>
        ))}
        {xTicks.map((tick, index) => (
          <text
            key={`x${String(index)}`}
            x={sx(tick)}
            y={height - 7}
            textAnchor={index === 0 ? "start" : index === xTicks.length - 1 ? "end" : "middle"}
            className="axis-label"
          >
            {xFormat(tick)}
          </text>
        ))}
        {baseline !== undefined || area ? (
          <line x1={MARGIN.left} x2={MARGIN.left + innerW} y1={zeroY} y2={zeroY} className="rl-baseline" />
        ) : null}
        {live.map((item) => {
          const first = item.points[0];
          const last = item.points[item.points.length - 1];
          return (
            <g key={item.id}>
              {area && first !== undefined && last !== undefined ? (
                <path
                  d={`${pathFor(item)}L${sx(last.x).toFixed(1)},${zeroY.toFixed(1)}L${sx(first.x).toFixed(1)},${zeroY.toFixed(1)}Z`}
                  fill={item.color}
                  opacity={0.18}
                />
              ) : null}
              <path
                d={pathFor(item)}
                fill="none"
                stroke={item.color}
                strokeWidth={1.6}
                strokeDasharray={item.dashed ? "5 3" : undefined}
                strokeLinejoin="round"
              />
            </g>
          );
        })}
        {hoverX !== null ? (
          <>
            <line x1={hoverX} x2={hoverX} y1={MARGIN.top} y2={MARGIN.top + innerH} className="rl-crosshair" />
            {tooltipRows.map(({ item, point }) =>
              point === undefined ? null : (
                <circle key={item.id} cx={sx(point.x)} cy={sy(point.y)} r={3.5} fill={item.color} stroke="#0b1015" />
              ),
            )}
          </>
        ) : null}
        <rect
          x={MARGIN.left}
          y={MARGIN.top}
          width={innerW}
          height={innerH}
          fill="transparent"
          onPointerMove={onMove}
        />
      </svg>
      {hoverPoint !== undefined && hoverX !== null ? (
        <div
          className="rl-tooltip"
          style={{ left: Math.min(width - 190, Math.max(0, hoverX + 12)), top: MARGIN.top }}
        >
          <div className="rl-tooltip-x">{xFormat(hoverPoint.x)}</div>
          {tooltipRows.map(({ item, point }) =>
            point === undefined ? null : (
              <div key={item.id}>
                <span className="rl-swatch" style={{ background: item.color }} aria-hidden="true" />
                {live.length > 1 ? `${item.label}: ` : ""}
                <strong>{yFormat(point.y)}</strong>
              </div>
            ),
          )}
        </div>
      ) : null}
    </div>
  );
}

export interface HistogramBin {
  lower: number;
  upper: number;
  count: number;
}

export function Histogram({
  bins,
  height = 200,
  valueFormat,
  ariaLabel,
}: {
  bins: HistogramBin[];
  height?: number;
  valueFormat: (value: number) => string;
  ariaLabel: string;
}) {
  const [ref, width] = useWidth();
  if (bins.length === 0) return <div className="chart-empty">No closed trades with a recorded P&amp;L.</div>;
  const innerW = Math.max(40, width - MARGIN.left - MARGIN.right);
  const innerH = height - MARGIN.top - MARGIN.bottom;
  const lo = bins[0]?.lower ?? 0;
  const hi = bins[bins.length - 1]?.upper ?? 1;
  const span = hi === lo ? 1 : hi - lo;
  const maxCount = Math.max(1, ...bins.map((bin) => bin.count));
  const sx = (x: number) => MARGIN.left + ((x - lo) / span) * innerW;
  const sy = (count: number) => MARGIN.top + (1 - count / maxCount) * innerH;
  const yTicks = niceTicks(0, maxCount, 4).filter((tick) => Number.isInteger(tick));
  const xTicks = [lo, ...(lo < 0 && hi > 0 ? [0] : []), hi];
  return (
    <div className="rl-chart-body" ref={ref}>
      <svg width={width} height={height} role="img" aria-label={ariaLabel}>
        <rect x={MARGIN.left} y={MARGIN.top} width={innerW} height={innerH} className="rl-plot-bg" />
        {yTicks.map((tick) => (
          <g key={`y${String(tick)}`}>
            <line x1={MARGIN.left} x2={MARGIN.left + innerW} y1={sy(tick)} y2={sy(tick)} className="rl-grid" />
            <text x={MARGIN.left - 6} y={sy(tick) + 3} textAnchor="end" className="axis-label">
              {tick}
            </text>
          </g>
        ))}
        {bins.map((bin) => {
          const x = sx(bin.lower);
          const w = Math.max(1, sx(bin.upper) - x - 1);
          const winner = bin.lower >= 0;
          const loser = bin.upper <= 0;
          return (
            <rect
              key={bin.lower}
              x={x}
              y={sy(bin.count)}
              width={w}
              height={Math.max(0, MARGIN.top + innerH - sy(bin.count))}
              fill={winner ? "#3fbf7f" : loser ? "#e0665a" : "#97a5b3"}
              opacity={0.88}
            >
              <title>{`${valueFormat(bin.lower)} to ${valueFormat(bin.upper)}: ${String(bin.count)} trade(s)`}</title>
            </rect>
          );
        })}
        {lo < 0 && hi > 0 ? (
          <line x1={sx(0)} x2={sx(0)} y1={MARGIN.top} y2={MARGIN.top + innerH} className="rl-baseline" />
        ) : null}
        {xTicks.map((tick, index) => (
          <text
            key={`x${String(index)}`}
            x={sx(tick)}
            y={height - 7}
            textAnchor={index === 0 ? "start" : index === xTicks.length - 1 ? "end" : "middle"}
            className="axis-label"
          >
            {valueFormat(tick)}
          </text>
        ))}
      </svg>
    </div>
  );
}

export interface BarDatum {
  label: string;
  value: number;
  /** Secondary value drawn as a tick (e.g. closed-trade P&L next to equity change). */
  note?: string;
}

export function BarChart({
  bars,
  height = 200,
  valueFormat,
  ariaLabel,
}: {
  bars: BarDatum[];
  height?: number;
  valueFormat: (value: number) => string;
  ariaLabel: string;
}) {
  const [ref, width] = useWidth();
  if (bars.length === 0) return <div className="chart-empty">No periods to display.</div>;
  const innerW = Math.max(40, width - MARGIN.left - MARGIN.right);
  const innerH = height - MARGIN.top - MARGIN.bottom;
  const values = bars.map((bar) => bar.value);
  const yMin = Math.min(0, ...values);
  const yMax = Math.max(0, ...values);
  const span = yMax === yMin ? 1 : yMax - yMin;
  const sy = (value: number) => MARGIN.top + (1 - (value - yMin) / span) * innerH;
  const band = innerW / bars.length;
  const yTicks = niceTicks(yMin, yMax, 5);
  const labelEvery = Math.max(1, Math.ceil(bars.length / Math.max(2, Math.floor(innerW / 64))));
  return (
    <div className="rl-chart-body" ref={ref}>
      <svg width={width} height={height} role="img" aria-label={ariaLabel}>
        <rect x={MARGIN.left} y={MARGIN.top} width={innerW} height={innerH} className="rl-plot-bg" />
        {yTicks.map((tick) => (
          <g key={`y${String(tick)}`}>
            <line x1={MARGIN.left} x2={MARGIN.left + innerW} y1={sy(tick)} y2={sy(tick)} className="rl-grid" />
            <text x={MARGIN.left - 6} y={sy(tick) + 3} textAnchor="end" className="axis-label">
              {valueFormat(tick)}
            </text>
          </g>
        ))}
        {bars.map((bar, index) => {
          const x = MARGIN.left + index * band + band * 0.12;
          const w = Math.max(1, band * 0.76);
          const y = Math.min(sy(bar.value), sy(0));
          return (
            <g key={bar.label}>
              <rect
                x={x}
                y={y}
                width={w}
                height={Math.max(1, Math.abs(sy(bar.value) - sy(0)))}
                fill={bar.value >= 0 ? "#3fbf7f" : "#e0665a"}
                opacity={0.9}
              >
                <title>{`${bar.label}: ${valueFormat(bar.value)}${bar.note ? ` (${bar.note})` : ""}`}</title>
              </rect>
              {index % labelEvery === 0 ? (
                <text x={x + w / 2} y={height - 7} textAnchor="middle" className="axis-label">
                  {bar.label.slice(5)}
                </text>
              ) : null}
            </g>
          );
        })}
        <line x1={MARGIN.left} x2={MARGIN.left + innerW} y1={sy(0)} y2={sy(0)} className="rl-baseline" />
      </svg>
    </div>
  );
}

/** Min / quartile / median / max strip for run-level return across a group. */
export function RangeStrip({
  domain,
  min,
  p25,
  median,
  p75,
  max,
  width = 170,
}: {
  domain: [number, number];
  min: number | null;
  p25: number | null;
  median: number | null;
  p75: number | null;
  max: number | null;
  width?: number;
}) {
  if (min === null || max === null || median === null) return <span className="unavailable">n/a</span>;
  const [lo, hi] = domain;
  const span = hi === lo ? 1 : hi - lo;
  const sx = (v: number) => 4 + ((v - lo) / span) * (width - 8);
  const q1 = p25 ?? median;
  const q3 = p75 ?? median;
  return (
    <svg
      width={width}
      height={22}
      role="img"
      aria-label={`Run return range ${min.toFixed(2)}% to ${max.toFixed(2)}%, median ${median.toFixed(2)}%`}
    >
      {lo < 0 && hi > 0 ? <line x1={sx(0)} x2={sx(0)} y1={2} y2={20} className="rl-baseline" /> : null}
      <line x1={sx(min)} x2={sx(max)} y1={11} y2={11} stroke="#97a5b3" strokeWidth={1.5} />
      <rect x={sx(q1)} y={6} width={Math.max(2, sx(q3) - sx(q1))} height={10} fill="#4da3ff" opacity={0.55} />
      <line x1={sx(median)} x2={sx(median)} y1={4} y2={18} stroke={median >= 0 ? "#6fdc9f" : "#ff9c90"} strokeWidth={2.5} />
    </svg>
  );
}

/** Horizontal bars, one per run, for a single metric (signed values supported). */
export function MetricBars({
  items,
  ariaLabel,
}: {
  items: Array<{ id: string; label: string; color: string; value: number | null; text: string; best: boolean }>;
  ariaLabel: string;
}) {
  const values = items.map((item) => Math.abs(item.value ?? 0));
  const scale = Math.max(1e-9, ...values);
  const hasNegative = items.some((item) => (item.value ?? 0) < 0);
  return (
    <ul className="rl-metric-bars" aria-label={ariaLabel}>
      {items.map((item) => {
        const widthPct = item.value === null ? 0 : (Math.abs(item.value) / scale) * (hasNegative ? 48 : 96);
        const negative = (item.value ?? 0) < 0;
        return (
          <li key={item.id}>
            <span className="rl-bar-label">{item.label}</span>
            <span className={`rl-bar-track${hasNegative ? " rl-bar-track-split" : ""}`}>
              {item.value === null ? null : (
                <span
                  className="rl-bar"
                  style={{
                    width: `${String(Math.max(1, widthPct))}%`,
                    background: item.color,
                    ...(hasNegative
                      ? negative
                        ? { right: "50%" }
                        : { left: "50%" }
                      : { left: 0 }),
                  }}
                />
              )}
            </span>
            <span className={`rl-bar-value${item.best ? " rl-best" : ""}`}>{item.text}</span>
          </li>
        );
      })}
    </ul>
  );
}
