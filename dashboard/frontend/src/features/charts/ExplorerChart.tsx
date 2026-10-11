/** Interactive candlestick chart for a single trade (Lightweight Charts).
 *
 * Only recorded data is drawn: candles/volume/VWAP from persisted bars, fills
 * and signals at their recorded timestamps and prices, and stop levels from
 * recorded stop events (or, lacking movements, the recorded initial/final
 * levels as plain price lines). Missing data is reported, never invented. */

import { useEffect, useMemo, useRef, useState } from "react";
import {
  CandlestickSeries,
  ColorType,
  CrosshairMode,
  HistogramSeries,
  LineSeries,
  LineStyle,
  LineType,
  TickMarkType,
  createChart,
  createSeriesMarkers,
  type IChartApi,
  type IPriceLine,
  type ISeriesApi,
  type ISeriesMarkersPluginApi,
  type SeriesMarker,
  type Time,
  type UTCTimestamp,
} from "lightweight-charts";
import "./charts.css";
import {
  EXCHANGE_TZ,
  formatAxisTime,
  slotAxisTime,
  slotFor,
  toExchangeAxisTime,
  tradeViewRange,
  type ChartMarker,
  type ChartModel,
} from "./chartModel";

export interface ChartFocus {
  epoch: number;
  nonce: number;
}

interface OverlayState {
  volume: boolean;
  vwap: boolean;
  fills: boolean;
  signals: boolean;
  stops: boolean;
  band: boolean;
}

const DEFAULT_OVERLAYS: OverlayState = { volume: true, vwap: true, fills: true, signals: true, stops: true, band: true };

const COLORS = {
  up: "#53c985",
  down: "#ef7567",
  vwap: "#f4c95d",
  stop: "#ff7edb",
  text: "#97a5b3",
  grid: "#1a232d",
  border: "#2c3945",
  bg: "#0b1015",
  buy: "#36d17c",
  cover: "#3ec9d6",
  sell: "#ff6257",
  short: "#ff9f43",
  signalLed: "#6fc7ff",
  signalIdle: "#6b7b8c",
  band: "rgba(77, 163, 255, 0.10)",
  focus: "rgba(255, 215, 94, 0.22)",
};

const t = (time: number): UTCTimestamp => time as UTCTimestamp;

function fillColor(marker: ChartMarker): string {
  switch (marker.action) {
    case "BUY": return COLORS.buy;
    case "COVER": return COLORS.cover;
    case "SELL": return COLORS.sell;
    case "SHORT": return COLORS.short;
    default: return marker.entry ? COLORS.buy : COLORS.sell;
  }
}

function toSeriesMarkers(model: ChartModel, show: OverlayState): SeriesMarker<Time>[] {
  const out: SeriesMarker<Time>[] = [];
  if (show.signals) {
    for (const s of model.signals) {
      const base = {
        id: s.id,
        time: t(s.time),
        shape: "circle" as const,
        size: 0.6,
        color: s.ledToFill ? COLORS.signalLed : COLORS.signalIdle,
        text: s.label,
      };
      // Signals with no recorded price are anchored to their bar, not a price.
      out.push(
        s.price === null
          ? { ...base, position: "aboveBar" as const }
          : { ...base, position: "atPriceMiddle" as const, price: s.price },
      );
    }
  }
  if (show.fills) {
    for (const f of model.fills) {
      if (f.price === null) continue;
      // Arrow tips sit on the recorded fill price: entries/covers point up from
      // below, sells/shorts point down from above.
      const up = f.action === "BUY" || f.action === "COVER" || (f.action === null && f.entry === true);
      out.push({
        id: f.id,
        time: t(f.time),
        position: up ? ("atPriceTop" as const) : ("atPriceBottom" as const),
        price: f.price,
        shape: up ? "arrowUp" : "arrowDown",
        size: 1.6,
        color: fillColor(f),
        text: `${f.label} @${f.price}`,
      });
    }
  }
  return out.sort((a, b) => (a.time as number) - (b.time as number));
}

interface Hover {
  x: number;
  y: number;
  time: number;
  o: number | null;
  h: number | null;
  l: number | null;
  c: number | null;
  v: number | null;
  vwap: number | null;
  items: { title: string; lines: string[] }[];
}

const fmt = (v: number | null, digits = 2) => (v === null ? "—" : v.toFixed(digits));

interface Props {
  model: ChartModel;
  symbol: string;
  focus: ChartFocus | null;
}

export default function ExplorerChart({ model, symbol, focus }: Props) {
  const containerRef = useRef<HTMLDivElement | null>(null);
  const [show, setShow] = useState<OverlayState>(DEFAULT_OVERLAYS);
  const [hover, setHover] = useState<Hover | null>(null);
  const showRef = useRef(show);
  showRef.current = show;

  const apiRef = useRef<{
    chart: IChartApi;
    candles: ISeriesApi<"Candlestick">;
    volume: ISeriesApi<"Histogram">;
    vwap: ISeriesApi<"Line">;
    stop: ISeriesApi<"Line">;
    band: ISeriesApi<"Histogram">;
    focus: ISeriesApi<"Histogram">;
    markers: ISeriesMarkersPluginApi<Time>;
    priceLines: IPriceLine[];
  } | null>(null);

  // Items (markers, stop steps) keyed by bar axis time for hover read-outs.
  const itemsByTime = useMemo(() => {
    const map = new Map<number, { title: string; lines: string[] }[]>();
    const add = (time: number, title: string, lines: string[]) => {
      const list = map.get(time) ?? [];
      list.push({ title, lines });
      map.set(time, list);
    };
    for (const f of model.fills) add(f.time, f.title, [...f.detail, `recorded ${formatAxisTime(toExchangeAxisTime(f.epoch), false, true)} ET`]);
    for (const s of model.signals) add(s.time, s.title, [...s.detail, `recorded ${formatAxisTime(toExchangeAxisTime(s.epoch), false, true)} ET`]);
    for (const step of model.stops.steps) add(step.time, `Stop: ${step.label}`, [`stop @ ${step.price}`]);
    return map;
  }, [model]);
  const itemsRef = useRef(itemsByTime);
  itemsRef.current = itemsByTime;

  // Build the chart and static data once per model.
  useEffect(() => {
    const container = containerRef.current;
    if (container === null || !model.chartAvailable) return;

    const chart = createChart(container, {
      autoSize: true,
      layout: { background: { type: ColorType.Solid, color: COLORS.bg }, textColor: COLORS.text, fontSize: 11 },
      grid: { vertLines: { color: COLORS.grid }, horzLines: { color: COLORS.grid } },
      crosshair: { mode: CrosshairMode.Normal },
      rightPriceScale: { borderColor: COLORS.border, scaleMargins: { top: 0.08, bottom: 0.22 } },
      timeScale: {
        borderColor: COLORS.border,
        timeVisible: true,
        secondsVisible: false,
        rightOffset: 4,
        tickMarkFormatter: (time: Time, type: TickMarkType) =>
          formatAxisTime(time as number, type === TickMarkType.Year || type === TickMarkType.Month || type === TickMarkType.DayOfMonth),
      },
      localization: {
        timeFormatter: (time: Time) => `${formatAxisTime(time as number, true, true)} ET`,
        priceFormatter: (price: number) => price.toFixed(2),
      },
      handleScroll: { mouseWheel: false, pressedMouseMove: true, horzTouchDrag: true, vertTouchDrag: false },
      handleScale: { mouseWheel: true, pinch: true, axisPressedMouseMove: true },
    });

    const band = chart.addSeries(HistogramSeries, {
      priceScaleId: "band", priceFormat: { type: "volume" }, lastValueVisible: false, priceLineVisible: false,
    });
    chart.priceScale("band").applyOptions({ visible: false, scaleMargins: { top: 0, bottom: 0 } });

    const focusSeries = chart.addSeries(HistogramSeries, {
      priceScaleId: "focus", lastValueVisible: false, priceLineVisible: false,
    });
    chart.priceScale("focus").applyOptions({ visible: false, scaleMargins: { top: 0, bottom: 0 } });

    const volume = chart.addSeries(HistogramSeries, {
      priceScaleId: "vol", priceFormat: { type: "volume" }, lastValueVisible: false, priceLineVisible: false,
    });
    chart.priceScale("vol").applyOptions({ scaleMargins: { top: 0.82, bottom: 0 } });

    const candles = chart.addSeries(CandlestickSeries, {
      upColor: COLORS.up, downColor: COLORS.down, wickUpColor: COLORS.up, wickDownColor: COLORS.down,
      borderVisible: false, priceLineVisible: false,
    });
    const vwap = chart.addSeries(LineSeries, {
      color: COLORS.vwap, lineWidth: 2, priceLineVisible: false, lastValueVisible: false,
      crosshairMarkerVisible: false, title: "VWAP",
    });
    const stop = chart.addSeries(LineSeries, {
      color: COLORS.stop, lineWidth: 2, lineStyle: LineStyle.Dashed, lineType: LineType.WithSteps,
      priceLineVisible: false, lastValueVisible: true, crosshairMarkerVisible: false, title: "stop",
    });

    // Slots without a recorded bar stay empty (whitespace) so gaps in the
    // recorded data are visible and the time axis stays true to the clock.
    const byTime = new Map(model.bars.candles.map((c) => [c.time, c]));
    const grid: ({ time: UTCTimestamp } | { time: UTCTimestamp; open: number; high: number; low: number; close: number })[] = [];
    for (let slot = 0; slot < model.bars.slotCount; slot += 1) {
      const time = slotAxisTime(model.bars, slot);
      const candle = byTime.get(time);
      grid.push(candle === undefined ? { time: t(time) } : { ...candle, time: t(time) });
    }
    candles.setData(grid);
    volume.setData(
      model.bars.volume.map((v) => ({
        time: t(v.time), value: v.value, color: v.up ? "rgba(83,201,133,0.45)" : "rgba(239,117,103,0.45)",
      })),
    );
    vwap.setData(model.bars.vwap.map((p) => ({ time: t(p.time), value: p.value })));

    const stepData = model.stops.steps.map((s) => ({ time: t(s.time), value: s.price }));
    const lastStep = model.stops.steps[model.stops.steps.length - 1];
    if (lastStep !== undefined && model.stops.endTime !== null && model.stops.endTime > lastStep.time) {
      stepData.push({ time: t(model.stops.endTime), value: lastStep.price });
    }
    stop.setData(stepData);

    if (model.entryIndex >= 0) {
      const last = model.exitIndex >= 0 ? model.exitIndex : model.bars.slotCount - 1;
      const points = [];
      for (let slot = model.entryIndex; slot <= last; slot += 1) {
        points.push({ time: t(slotAxisTime(model.bars, slot)), value: 1, color: COLORS.band });
      }
      band.setData(points);
    }

    const markers = createSeriesMarkers(candles, [] as SeriesMarker<Time>[]);

    chart.subscribeCrosshairMove((param) => {
      if (param.time === undefined || param.point === undefined) {
        setHover(null);
        return;
      }
      const time = param.time as number;
      const bar = param.seriesData.get(candles) as { open: number; high: number; low: number; close: number } | undefined;
      const vol = param.seriesData.get(volume) as { value: number } | undefined;
      const vw = param.seriesData.get(vwap) as { value: number } | undefined;
      setHover({
        x: param.point.x,
        y: param.point.y,
        time,
        o: bar?.open ?? null,
        h: bar?.high ?? null,
        l: bar?.low ?? null,
        c: bar?.close ?? null,
        v: vol?.value ?? null,
        vwap: vw?.value ?? null,
        items: itemsRef.current.get(time) ?? [],
      });
    });

    apiRef.current = { chart, candles, volume, vwap, stop, band, focus: focusSeries, markers, priceLines: [] };
    const range = tradeViewRange(model);
    if (range !== null) chart.timeScale().setVisibleLogicalRange(range);
    else chart.timeScale().fitContent();

    return () => {
      apiRef.current = null;
      chart.remove();
    };
  }, [model]);

  // Overlay visibility (cheap; re-applied when toggles or model change).
  useEffect(() => {
    const api = apiRef.current;
    if (api === null) return;
    api.volume.applyOptions({ visible: show.volume });
    api.vwap.applyOptions({ visible: show.vwap && model.hasVwap });
    api.band.applyOptions({ visible: show.band });
    api.stop.applyOptions({ visible: show.stops && model.stops.recordedMovements });
    api.markers.setMarkers(toSeriesMarkers(model, show));
    for (const line of api.priceLines) api.candles.removePriceLine(line);
    api.priceLines = [];
    // Without recorded movements, show the recorded levels only as reference
    // price lines — never as a drawn trailing path.
    if (show.stops && !model.stops.recordedMovements) {
      const { initial, final } = model.stops;
      const make = (price: number, title: string) =>
        api.candles.createPriceLine({ price, color: COLORS.stop, lineWidth: 1, lineStyle: LineStyle.Dashed, axisLabelVisible: true, title });
      if (initial !== null && final !== null && initial === final) api.priceLines.push(make(initial, "stop (recorded)"));
      else {
        if (initial !== null) api.priceLines.push(make(initial, "initial stop (recorded)"));
        if (final !== null) api.priceLines.push(make(final, "final stop (recorded)"));
      }
    }
  }, [show, model]);

  // External focus (timeline click): highlight the bar and bring it into view.
  useEffect(() => {
    const api = apiRef.current;
    if (api === null) return;
    if (focus === null) {
      api.focus.setData([]);
      return;
    }
    const idx = slotFor(model.bars, focus.epoch);
    if (idx < 0) {
      api.focus.setData([]);
      return;
    }
    api.focus.setData([{ time: t(slotAxisTime(model.bars, idx)), value: 1, color: COLORS.focus }]);
    const visible = api.chart.timeScale().getVisibleLogicalRange();
    const width = visible === null ? 60 : visible.to - visible.from;
    api.chart.timeScale().setVisibleLogicalRange({ from: idx - width / 2, to: idx + width / 2 });
  }, [focus, model]);

  const toggle = (key: keyof OverlayState) => setShow((s) => ({ ...s, [key]: !s[key] }));

  const fitTrade = () => {
    const api = apiRef.current;
    if (api === null) return;
    const range = tradeViewRange(model);
    if (range === null) api.chart.timeScale().fitContent();
    else api.chart.timeScale().setVisibleLogicalRange(range);
    api.chart.priceScale("right").applyOptions({ autoScale: true });
  };
  const fullSession = () => {
    const api = apiRef.current;
    if (api === null) return;
    api.chart.timeScale().fitContent();
    api.chart.priceScale("right").applyOptions({ autoScale: true });
  };

  if (!model.chartAvailable) {
    return (
      <div className="tx-chart-empty" role="status">
        <strong>Chart unavailable</strong>
        <p>{model.unavailableReason}</p>
        <p className="hint">Trade details, fills and the event timeline below remain authoritative. No candles or fill prices are synthesized.</p>
      </div>
    );
  }

  const toggles: { key: keyof OverlayState; label: string; swatch: string; disabled?: string }[] = [
    { key: "fills", label: "Fills", swatch: COLORS.buy, disabled: model.fills.length === 0 ? "No placeable fills" : undefined },
    { key: "signals", label: "Signals", swatch: COLORS.signalLed, disabled: model.signals.length === 0 ? "No signals recorded for this trade" : undefined },
    { key: "stops", label: "Stops", swatch: COLORS.stop, disabled: model.stops.recordedMovements || model.stops.initial !== null || model.stops.final !== null ? undefined : "No stop levels recorded" },
    { key: "vwap", label: "VWAP", swatch: COLORS.vwap, disabled: model.hasVwap ? undefined : "VWAP not recorded in this dataset" },
    { key: "volume", label: "Volume", swatch: "#5b7186" },
    { key: "band", label: "Holding window", swatch: "#4da3ff", disabled: model.entryAxisTime === null ? "Entry time not on chart" : undefined },
  ];

  return (
    <div className="tx-chart">
      <div className="tx-chart-toolbar">
        <div className="tx-toggle-group" role="group" aria-label="Chart overlays">
          {toggles.map((item) => (
            <button
              key={item.key}
              type="button"
              className={`tx-toggle${show[item.key] && item.disabled === undefined ? " on" : ""}`}
              aria-pressed={show[item.key] && item.disabled === undefined}
              disabled={item.disabled !== undefined}
              title={item.disabled}
              onClick={() => toggle(item.key)}
            >
              <i style={{ background: item.swatch }} />
              {item.label}
            </button>
          ))}
        </div>
        <div className="tx-toggle-group">
          <button type="button" onClick={fitTrade} title="Frame the trade with surrounding context">Fit trade</button>
          <button type="button" onClick={fullSession}>Full session</button>
        </div>
      </div>
      <div className="tx-chart-stage">
        <div ref={containerRef} className="tx-chart-canvas" role="img" aria-label={`${symbol} candlestick chart, ${EXCHANGE_TZ} time`} />
        <div className="tx-readout" aria-live="off">
          <b>{symbol}</b>
          {hover === null ? (
            <span className="muted">Hover the chart for OHLCV · drag to pan · wheel to zoom</span>
          ) : (
            <>
              <span>{formatAxisTime(hover.time, true)} ET</span>
              <span>O {fmt(hover.o)}</span>
              <span>H {fmt(hover.h)}</span>
              <span>L {fmt(hover.l)}</span>
              <span>C {fmt(hover.c)}</span>
              <span>Vol {hover.v === null ? "—" : hover.v.toLocaleString("en-US")}</span>
              {hover.vwap !== null ? <span style={{ color: COLORS.vwap }}>VWAP {fmt(hover.vwap)}</span> : null}
            </>
          )}
        </div>
        {hover !== null && hover.items.length > 0 ? (
          <div
            className="tx-tooltip"
            style={{ left: Math.min(hover.x + 14, (containerRef.current?.clientWidth ?? 600) - 250), top: Math.max(hover.y - 8, 34) }}
          >
            {hover.items.map((item, i) => (
              <div key={i}>
                <strong>{item.title}</strong>
                {item.lines.map((line, j) => <div key={j}>{line}</div>)}
              </div>
            ))}
          </div>
        ) : null}
      </div>
      <div className="tx-legend">
        <span><i className="arrow up" style={{ borderBottomColor: COLORS.buy }} />BUY / COVER fill</span>
        <span><i className="arrow down" style={{ borderTopColor: COLORS.sell }} />SELL / SHORT fill</span>
        <span><i className="dot" style={{ background: COLORS.signalLed }} />signal that led to a fill</span>
        <span><i className="dot" style={{ background: COLORS.signalIdle }} />signal, no fill</span>
        <span>Times in {EXCHANGE_TZ}</span>
      </div>
      {model.unplaced.length > 0 ? (
        <p className="hint tx-unplaced">
          {model.unplaced.length} recorded item(s) could not be placed on the chart ({[...new Set(model.unplaced.map((u) => u.reason))].join("; ")}); they remain in the tables below.
        </p>
      ) : null}
    </div>
  );
}
