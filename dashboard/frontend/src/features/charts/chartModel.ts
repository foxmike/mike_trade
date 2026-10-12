/** Pure data transformation from a TradeInspection into chart-ready series.
 *
 * Everything here is derived from recorded data only. Nothing is interpolated
 * or invented: unparseable numbers drop the affected point, stops are drawn as
 * steps only when stop events were recorded, and fills without a matching bar
 * stay in the tables but are not placed on the chart. */

import type { TradeBar, TradeEvent, TradeFill, TradeInspection } from "../../api/client";

export const EXCHANGE_TZ = "America/New_York";
export const BAR_SECONDS = 60;

export type Direction = "LONG" | "SHORT" | "UNKNOWN";
export type FillAction = "BUY" | "SELL" | "SHORT" | "COVER";

export function num(value: string | number | null | undefined): number | null {
  if (value === null || value === undefined || value === "") return null;
  const parsed = typeof value === "number" ? value : Number(value);
  return Number.isFinite(parsed) ? parsed : null;
}

/** Epoch seconds from an ISO timestamp, or null when unparseable. */
export function epochSeconds(value: string | null | undefined): number | null {
  if (value === null || value === undefined || value === "") return null;
  const ms = new Date(value).getTime();
  return Number.isFinite(ms) ? Math.floor(ms / 1000) : null;
}

const offsetFormat = new Intl.DateTimeFormat("en-US", {
  timeZone: EXCHANGE_TZ,
  hourCycle: "h23",
  year: "numeric",
  month: "2-digit",
  day: "2-digit",
  hour: "2-digit",
  minute: "2-digit",
  second: "2-digit",
});

/** Shift a true epoch time so that a UTC-based axis displays exchange-local
 * wall-clock time. Lightweight Charts formats times as UTC, so the shifted
 * value is only ever used for charting, never for display of raw instants. */
export function toExchangeAxisTime(epochSec: number): number {
  const parts = offsetFormat.formatToParts(new Date(epochSec * 1000));
  const get = (type: string): number => Number(parts.find((p) => p.type === type)?.value ?? 0);
  const wall = Date.UTC(get("year"), get("month") - 1, get("day"), get("hour"), get("minute"), get("second")) / 1000;
  return wall;
}

/** Format a shifted axis time as exchange-local text. */
export function formatAxisTime(axisTime: number, withDate = false, withSeconds = false): string {
  const d = new Date(axisTime * 1000);
  const pad = (n: number) => String(n).padStart(2, "0");
  const hm = `${pad(d.getUTCHours())}:${pad(d.getUTCMinutes())}${withSeconds ? `:${pad(d.getUTCSeconds())}` : ""}`;
  if (!withDate) return hm;
  return `${d.getUTCFullYear()}-${pad(d.getUTCMonth() + 1)}-${pad(d.getUTCDate())} ${hm}`;
}

export function normalizeFillAction(side: string | null | undefined): FillAction | null {
  const s = (side ?? "").toUpperCase().replace(/[\s-]+/g, "_");
  if (s === "BUY") return "BUY";
  if (s === "SELL") return "SELL";
  if (s === "SELL_SHORT" || s === "SHORT" || s === "SELLSHORT") return "SHORT";
  if (s === "BUY_TO_COVER" || s === "COVER" || s === "BUYTOCOVER") return "COVER";
  return null;
}

export function isEntryAction(action: FillAction): boolean {
  return action === "BUY" || action === "SHORT";
}

/** SHORT when any fill is a short sale / cover, LONG when buys exist,
 * otherwise UNKNOWN (never guessed from a lone SELL). */
export function inferDirection(fills: readonly Pick<TradeFill, "side">[]): Direction {
  const actions = fills.map((f) => normalizeFillAction(f.side));
  if (actions.includes("SHORT") || actions.includes("COVER")) return "SHORT";
  if (actions.includes("BUY")) return "LONG";
  return "UNKNOWN";
}

export interface CandlePoint { time: number; open: number; high: number; low: number; close: number }
export interface VolumePoint { time: number; value: number; up: boolean }
export interface LinePoint { time: number; value: number }

export interface ChartBars {
  candles: CandlePoint[];
  volume: VolumePoint[];
  vwap: LinePoint[];
  /** Real epoch seconds per candle, parallel to `candles`. */
  epochs: number[];
  /** Epoch of the first recorded bar; the time grid is one slot per minute from here. */
  baseEpoch: number;
  /** Number of one-minute slots from the first to the last recorded bar, inclusive.
   * Slots without a recorded bar are drawn as gaps, never filled with candles. */
  slotCount: number;
}

/** Convert API bars; bars with unusable OHLC are dropped, duplicates removed. */
export function buildBars(bars: readonly TradeBar[]): ChartBars {
  const rows: { epoch: number; bar: TradeBar; o: number; h: number; l: number; c: number }[] = [];
  for (const bar of bars) {
    const epoch = epochSeconds(bar.timestamp);
    const o = num(bar.open), h = num(bar.high), l = num(bar.low), c = num(bar.close);
    if (epoch === null || o === null || h === null || l === null || c === null) continue;
    rows.push({ epoch, bar, o, h, l, c });
  }
  rows.sort((a, b) => a.epoch - b.epoch);
  const out: ChartBars = { candles: [], volume: [], vwap: [], epochs: [], baseEpoch: 0, slotCount: 0 };
  let previous = -1;
  for (const row of rows) {
    if (row.epoch === previous) continue;
    previous = row.epoch;
    const time = toExchangeAxisTime(row.epoch);
    out.epochs.push(row.epoch);
    out.candles.push({ time, open: row.o, high: row.h, low: row.l, close: row.c });
    const volume = num(row.bar.volume);
    if (volume !== null) out.volume.push({ time, value: volume, up: row.c >= row.o });
    const vwap = num(row.bar.vwap);
    if (vwap !== null) out.vwap.push({ time, value: vwap });
  }
  if (out.epochs.length > 0) {
    out.baseEpoch = out.epochs[0] as number;
    out.slotCount = Math.floor(((out.epochs[out.epochs.length - 1] as number) - out.baseEpoch) / BAR_SECONDS) + 1;
  }
  return out;
}

/** One-minute slot (0-based from the first recorded bar) containing `epoch`, or
 * -1 when it falls outside the recorded session. Slots may lack a bar. */
export function slotFor(bars: Pick<ChartBars, "baseEpoch" | "slotCount">, epoch: number | null): number {
  if (epoch === null || bars.slotCount === 0 || epoch < bars.baseEpoch) return -1;
  const slot = Math.floor((epoch - bars.baseEpoch) / BAR_SECONDS);
  return slot < bars.slotCount ? slot : -1;
}

/** Axis time of a slot's start. */
export function slotAxisTime(bars: Pick<ChartBars, "baseEpoch">, slot: number): number {
  return toExchangeAxisTime(bars.baseEpoch + slot * BAR_SECONDS);
}

export type OverlayKind = "fill" | "signal";

export interface ChartMarker {
  id: string;
  kind: OverlayKind;
  /** Bar-aligned shifted axis time. */
  time: number;
  /** Real epoch seconds of the recorded event. */
  epoch: number;
  /** Recorded price, or null when none exists (signals without a reference price are bar-anchored). */
  price: number | null;
  label: string;
  title: string;
  detail: string[];
  action: FillAction | null;
  /** Fill is an entry fill (BUY/SHORT); false for exits; null for signals. */
  entry: boolean | null;
  /** Signal that is the recorded entry/exit signal of this trade. */
  ledToFill: boolean;
  /** Marker sourced from the trade record rather than a fill row. */
  fromTradeRecord: boolean;
}

export interface StopStep { time: number; epoch: number; price: number; label: string }

export interface StopModel {
  /** Recorded stop levels over time (events). Empty when none were recorded. */
  steps: StopStep[];
  /** Fallback single levels from the trade record, used only without steps. */
  initial: number | null;
  final: number | null;
  /** Axis time at which the step line should end (exit or last bar). */
  endTime: number | null;
  recordedMovements: boolean;
}

export interface UnplacedItem { id: string; kind: string; reason: string }

export interface ChartModel {
  bars: ChartBars;
  direction: Direction;
  fills: ChartMarker[];
  signals: ChartMarker[];
  stops: StopModel;
  entryAxisTime: number | null;
  exitAxisTime: number | null;
  entryIndex: number;
  exitIndex: number;
  unplaced: UnplacedItem[];
  hasVwap: boolean;
  chartAvailable: boolean;
  unavailableReason: string | null;
}

const STOP_EVENT = /STOP|PROTECT/i;

export function isStopEvent(event: Pick<TradeEvent, "event_type" | "price">): boolean {
  return STOP_EVENT.test(event.event_type) && num(event.price) !== null;
}

function snap(bars: ChartBars, epoch: number | null): { index: number; time: number } | null {
  const index = slotFor(bars, epoch);
  return index < 0 ? null : { index, time: slotAxisTime(bars, index) };
}

function fillDetail(fill: TradeFill): string[] {
  return [`${fill.side} ${fill.quantity} @ ${fill.price}`, fill.fee && num(fill.fee) !== 0 ? `fee ${fill.fee}` : ""].filter(Boolean);
}

export function buildChartModel(inspection: TradeInspection): ChartModel {
  const { trade, fills, events } = inspection;
  const bars = buildBars(inspection.bars);
  const recordedDirection = String((trade as { direction?: unknown }).direction ?? "").toUpperCase();
  const direction: Direction =
    recordedDirection === "LONG" || recordedDirection === "SHORT" ? recordedDirection : inferDirection(fills);
  const unplaced: UnplacedItem[] = [];

  const fillMarkers: ChartMarker[] = [];
  for (const fill of fills) {
    const action = normalizeFillAction(fill.side);
    const epoch = epochSeconds(fill.timestamp);
    const price = num(fill.price);
    if (action === null || epoch === null || price === null) {
      unplaced.push({ id: fill.fill_id, kind: "fill", reason: "fill has an unrecognised side, time or price" });
      continue;
    }
    const at = snap(bars, epoch);
    if (at === null) {
      unplaced.push({ id: fill.fill_id, kind: "fill", reason: "fill time is outside the recorded bars" });
      continue;
    }
    const qty = num(fill.quantity);
    fillMarkers.push({
      id: `fill:${fill.fill_id}`,
      kind: "fill",
      time: at.time,
      epoch,
      price,
      label: `${action}${qty !== null ? ` ${qty}` : ""}`,
      title: `${action} fill`,
      detail: fillDetail(fill),
      action,
      entry: isEntryAction(action),
      ledToFill: true,
      fromTradeRecord: false,
    });
  }

  // Without fill rows, fall back to the recorded trade-level entry/exit, labelled
  // as such. Direction is unknown here, so the label stays neutral.
  if (fills.length === 0) {
    const legs: [string, string | null, string | null, boolean][] = [
      ["entry", trade.entry_timestamp, trade.entry_price, true],
      ["exit", trade.exit_timestamp, trade.exit_price, false],
    ];
    for (const [name, ts, px, entry] of legs) {
      const epoch = epochSeconds(ts);
      const price = num(px);
      const at = snap(bars, epoch);
      if (epoch === null || price === null || at === null) continue;
      fillMarkers.push({
        id: `record:${name}`,
        kind: "fill",
        time: at.time,
        epoch,
        price,
        label: name.toUpperCase(),
        title: `${name} (trade record)`,
        detail: [`${name} @ ${px}`, "No individual fill rows recorded"],
        action: null,
        entry,
        ledToFill: true,
        fromTradeRecord: true,
      });
    }
  }

  const ledSignalIds = new Set([trade.entry_signal_id, trade.exit_signal_id].filter((v): v is string => !!v));
  const signalMarkers: ChartMarker[] = [];
  events.forEach((event, index) => {
    if (event.event_type !== "SIGNAL_GENERATED") return;
    const epoch = epochSeconds(event.timestamp);
    const price = num(event.price);
    const id = `signal:${event.signal_id ?? index}:${index}`;
    if (epoch === null) {
      unplaced.push({ id, kind: "signal", reason: "signal has an unparseable time" });
      return;
    }
    const at = snap(bars, epoch);
    if (at === null) {
      unplaced.push({ id, kind: "signal", reason: "signal time is outside the recorded bars" });
      return;
    }
    const led = event.signal_id !== null && ledSignalIds.has(event.signal_id);
    signalMarkers.push({
      id,
      kind: "signal",
      time: at.time,
      epoch,
      price,
      label: led ? "sig" : "",
      title: `${event.source ?? "Signal"} signal${led ? " (led to fill)" : " (no fill)"}`,
      detail: [
        [event.strategy_id, event.strategy_version ? `v${event.strategy_version}` : null].filter(Boolean).join(" "),
        price !== null ? `reference @ ${event.price}` : "no reference price recorded (marker is bar-anchored)",
        event.reason ?? "",
      ].filter(Boolean),
      action: null,
      entry: null,
      ledToFill: led,
      fromTradeRecord: false,
    });
  });

  // Stops: only recorded events produce a stepped line.
  const stopEvents = events
    .filter(isStopEvent)
    .map((event) => ({ event, epoch: epochSeconds(event.timestamp), price: num(event.price) }))
    .filter((row): row is { event: TradeEvent; epoch: number; price: number } => row.epoch !== null && row.price !== null)
    .sort((a, b) => a.epoch - b.epoch);
  const steps: StopStep[] = [];
  for (const row of stopEvents) {
    let at = snap(bars, row.epoch);
    // A stop placed before the first recorded bar still applies from that bar.
    if (at === null && bars.slotCount > 0 && row.epoch < bars.baseEpoch) {
      at = { index: 0, time: slotAxisTime(bars, 0) };
    }
    if (at === null) continue;
    const label = row.event.event_type.replace(/_/g, " ").toLowerCase();
    const previous = steps[steps.length - 1];
    if (previous !== undefined && previous.time === at.time) {
      steps[steps.length - 1] = { time: at.time, epoch: row.epoch, price: row.price, label };
    } else {
      steps.push({ time: at.time, epoch: row.epoch, price: row.price, label });
    }
  }

  const entryEpoch = epochSeconds(trade.entry_timestamp);
  const exitEpoch = epochSeconds(trade.exit_timestamp);
  const entryAt = snap(bars, entryEpoch);
  const exitAt = snap(bars, exitEpoch);
  const endTime = exitAt?.time ?? (bars.slotCount > 0 ? slotAxisTime(bars, bars.slotCount - 1) : null);

  const chartAvailable = bars.candles.length > 0;
  return {
    bars,
    direction,
    fills: fillMarkers,
    signals: signalMarkers,
    stops: {
      steps,
      initial: num(trade.initial_stop_price),
      final: num(trade.final_stop_price),
      endTime,
      recordedMovements: steps.length > 0,
    },
    entryAxisTime: entryAt?.time ?? null,
    exitAxisTime: exitAt?.time ?? null,
    entryIndex: entryAt?.index ?? -1,
    exitIndex: exitAt?.index ?? -1,
    unplaced,
    hasVwap: bars.vwap.length > 0,
    chartAvailable,
    unavailableReason: chartAvailable
      ? null
      : inspection.bars_limitation ?? "No recorded market bars are available for this trade.",
  };
}

/** Logical bar range that frames a trade with context on both sides. */
export function tradeViewRange(
  model: Pick<ChartModel, "entryIndex" | "exitIndex" | "bars">,
  padBars = 20,
): { from: number; to: number } | null {
  const count = model.bars.slotCount;
  if (count === 0) return null;
  const first = model.entryIndex >= 0 ? model.entryIndex : model.exitIndex;
  if (first < 0) return null;
  const last = model.exitIndex >= 0 ? model.exitIndex : count - 1;
  const span = Math.max(1, last - first);
  const pad = Math.max(padBars, Math.round(span * 0.35));
  return { from: first - pad, to: last + pad };
}
