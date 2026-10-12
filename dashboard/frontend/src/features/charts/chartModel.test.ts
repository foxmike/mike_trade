import { describe, expect, it } from "vitest";
import type { TradeBar, TradeEvent, TradeFill, TradeInspection, TradeRecord } from "../../api/client";
import {
  slotFor,
  buildChartModel,
  formatAxisTime,
  inferDirection,
  normalizeFillAction,
  toExchangeAxisTime,
  tradeViewRange,
} from "./chartModel";

// 2026-03-02 is EST (UTC-5): 09:30 ET == 14:30Z. 2026-07-01 is EDT (UTC-4).
const iso = (hhmm: string, date = "2026-03-02", z = "Z") => `${date}T${hhmm}:00${z}`;

function bar(timestamp: string, extra: Partial<TradeBar> = {}): TradeBar {
  return {
    timestamp, timestamp_et: timestamp, open: "10", high: "11", low: "9", close: "10.5",
    vwap: "10.2", volume: "1000", feed: null, provenance: null, ...extra,
  };
}

function fill(id: string, side: string, timestamp: string, price: string, quantity = "100"): TradeFill {
  return { fill_id: id, order_id: null, side, timestamp, price, quantity, fee: "0" };
}

function event(type: string, timestamp: string, extra: Partial<TradeEvent> = {}): TradeEvent {
  return {
    timestamp, timestamp_et: timestamp, event_type: type, source: null, strategy_id: null,
    strategy_version: null, signal_id: null, order_id: null, price: null, quantity: null,
    reason: null, details: {}, ...extra,
  };
}

function trade(extra: Partial<TradeRecord> = {}): TradeRecord {
  return {
    trade_id: "t1", execution_mode: "BACKTEST", account_id: "a", scenario_id: null, research_run_id: "r",
    symbol: "ABC", cycle_id: "c1", entry_timestamp: iso("14:35"), exit_timestamp: iso("14:50"),
    entry_price: "10", exit_price: "11", filled_quantity: "100", exited_quantity: "100", open_quantity: "0",
    entry_notional: null, exit_notional: null, realized_pnl: "100", unrealized_pnl: null,
    realized_return_percent: "10", position_status: "CLOSED", entry_strategy_id: "s", entry_strategy_version: "1",
    entry_signal_id: "sig-entry", entry_signal_timestamp: null, entry_decision: null, exit_strategy_id: null,
    exit_strategy_version: null, exit_signal_id: null, exit_signal_timestamp: null, exit_reason: "STOP",
    protective_stop_mode: null, initial_stop_price: null, final_stop_price: null, holding_duration_seconds: null,
    data_provenance: null, attribution_complete: true, ...extra,
  };
}

function inspection(extra: Partial<TradeInspection> = {}): TradeInspection {
  const bars = ["14:30", "14:31", "14:32", "14:33", "14:34", "14:35", "14:36", "14:50"].map((t) => bar(iso(t)));
  return {
    trade: trade(), fills: [], bars, events: [],
    diagnostics: {} as TradeInspection["diagnostics"], bars_available: true, bars_limitation: null,
    timezone: "America/New_York", ...extra,
  };
}

describe("exchange-local axis time", () => {
  it("renders 14:30Z as 09:30 in winter and 10:30 UTC-4 equivalents in summer", () => {
    expect(formatAxisTime(toExchangeAxisTime(Date.parse("2026-03-02T14:30:00Z") / 1000))).toBe("09:30");
    expect(formatAxisTime(toExchangeAxisTime(Date.parse("2026-07-01T13:30:00Z") / 1000))).toBe("09:30");
  });
});

describe("fill normalisation and direction", () => {
  it("maps broker sides to chart actions", () => {
    expect(normalizeFillAction("sell_short")).toBe("SHORT");
    expect(normalizeFillAction("BUY_TO_COVER")).toBe("COVER");
    expect(normalizeFillAction("weird")).toBeNull();
  });
  it("infers SHORT, LONG and never guesses from a lone SELL", () => {
    expect(inferDirection([{ side: "SELL_SHORT" }, { side: "BUY_TO_COVER" }])).toBe("SHORT");
    expect(inferDirection([{ side: "BUY" }, { side: "SELL" }])).toBe("LONG");
    expect(inferDirection([{ side: "SELL" }])).toBe("UNKNOWN");
  });
});

describe("buildChartModel", () => {
  it("places fills on their bar at the recorded price and keeps unmatched ones aside", () => {
    const model = buildChartModel(
      inspection({
        fills: [
          fill("f1", "SELL_SHORT", "2026-03-02T14:35:27Z", "10.12"),
          fill("f2", "BUY_TO_COVER", "2026-03-02T14:50:05Z", "9.5"),
          fill("f3", "BUY_TO_COVER", "2026-03-02T18:00:00Z", "9.4"),
        ],
      }),
    );
    expect(model.direction).toBe("SHORT");
    expect(model.fills.map((f) => [f.action, f.price, f.entry])).toEqual([
      ["SHORT", 10.12, true],
      ["COVER", 9.5, false],
    ]);
    expect(model.fills[0]?.time).toBe(toExchangeAxisTime(Date.parse("2026-03-02T14:35:00Z") / 1000));
    expect(model.unplaced).toHaveLength(1);
  });

  it("flags signals that led to a fill and keeps unpriced signals bar-anchored", () => {
    const model = buildChartModel(
      inspection({
        events: [
          event("SIGNAL_GENERATED", iso("14:34"), { signal_id: "sig-entry", price: "10.05" }),
          event("SIGNAL_GENERATED", iso("14:36"), { signal_id: "other", price: "10.4" }),
          event("SIGNAL_GENERATED", iso("14:36"), { signal_id: "nope", price: null }),
        ],
      }),
    );
    expect(model.signals.map((s) => s.ledToFill)).toEqual([true, false, false]);
    // Unpriced signals stay bar-anchored (price null) rather than getting an invented price.
    expect(model.signals.map((s) => s.price)).toEqual([10.05, 10.4, null]);
    expect(model.signals[2]?.detail.join(" ")).toContain("no reference price");
  });

  it("draws stop steps only from recorded events and ignores null prices", () => {
    const model = buildChartModel(
      inspection({
        trade: trade({ initial_stop_price: null, final_stop_price: null }),
        events: [
          event("INITIAL_STOP_ESTABLISHED", iso("14:35"), { price: "9.8" }),
          event("STOP_MOVED", iso("14:36"), { price: "9.9" }),
          event("STOP_MOVED", iso("14:36:00".slice(0, 5)), { price: null }),
        ],
      }),
    );
    expect(model.stops.recordedMovements).toBe(true);
    expect(model.stops.steps.map((s) => s.price)).toEqual([9.8, 9.9]);
    expect(model.stops.initial).toBeNull();
    expect(model.stops.final).toBeNull();
  });

  it("falls back to recorded levels (no steps) when no movements exist", () => {
    const model = buildChartModel(inspection({ trade: trade({ initial_stop_price: "9.5", final_stop_price: "9.5" }) }));
    expect(model.stops.recordedMovements).toBe(false);
    expect(model.stops.initial).toBe(9.5);
  });

  it("reports the chart as unavailable when there are no bars", () => {
    const model = buildChartModel(inspection({ bars: [], bars_available: false, bars_limitation: "dataset missing" }));
    expect(model.chartAvailable).toBe(false);
    expect(model.unavailableReason).toBe("dataset missing");
    expect(model.fills).toEqual([]);
  });

  it("drops bars with unusable numbers and tolerates missing vwap", () => {
    const model = buildChartModel(
      inspection({ bars: [bar(iso("14:30"), { open: "NaN" }), bar(iso("14:31"), { vwap: null }), bar(iso("14:31"))] }),
    );
    expect(model.bars.candles).toHaveLength(1);
    expect(model.bars.vwap).toEqual([]);
    expect(model.hasVwap).toBe(false);
  });

  it("prefers the recorded direction over inference", () => {
    const withDirection = trade({ ...({ direction: "SHORT" } as object) });
    expect(buildChartModel(inspection({ trade: withDirection, fills: [fill("f", "BUY", iso("14:35"), "10")] })).direction).toBe("SHORT");
  });

  it("uses trade-record entry/exit only when no fill rows exist", () => {
    const model = buildChartModel(inspection());
    expect(model.fills.map((f) => [f.fromTradeRecord, f.entry])).toEqual([[true, true], [true, false]]);
  });
});

describe("view helpers", () => {
  it("maps times to one-minute slots, keeping gaps and rejecting out-of-session times", () => {
    const grid = { baseEpoch: 1000, slotCount: 4 };
    expect(slotFor(grid, 1000)).toBe(0);
    expect(slotFor(grid, 1000 + 119)).toBe(1);
    expect(slotFor(grid, 1000 + 4 * 60)).toBe(-1);
    expect(slotFor(grid, 999)).toBe(-1);
    expect(slotFor(grid, null)).toBe(-1);
  });
  it("places a fill that falls in a gap between recorded bars", () => {
    const sparse = inspection({
      bars: [bar(iso("14:30")), bar(iso("14:35"))],
      fills: [fill("gap", "BUY", "2026-03-02T14:33:10Z", "10.1")],
    });
    const model = buildChartModel(sparse);
    expect(model.bars.slotCount).toBe(6);
    expect(model.fills.map((f) => f.price)).toEqual([10.1]);
  });
  it("frames the trade with padding", () => {
    const model = buildChartModel(inspection());
    const range = tradeViewRange(model, 5);
    expect(range).not.toBeNull();
    expect(range!.from).toBeLessThan(model.entryIndex);
    expect(range!.to).toBeGreaterThan(model.exitIndex);
  });
});
