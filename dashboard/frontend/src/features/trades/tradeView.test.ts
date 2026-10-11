import { describe, expect, it } from "vitest";
import { contextQuery, exitLabel, toneOf } from "./tradeView";
import type { TradeRecord } from "../../api/client";

describe("tradeView", () => {
  it("toneOf handles null and non-numeric values", () => {
    expect(toneOf(null)).toBeUndefined();
    expect(toneOf("abc")).toBeUndefined();
    expect(toneOf("0")).toBeUndefined();
    expect(toneOf("1.5")).toBe("positive");
    expect(toneOf("-1.5")).toBe("negative");
  });
  it("exitLabel prefers the strategy for strategy exits, else the protective reason", () => {
    const base = { exit_timestamp: "x", exit_strategy_version: "2" } as TradeRecord;
    expect(exitLabel({ ...base, exit_reason: "STRATEGY_EXIT", exit_strategy_id: "vwap_exit" } as TradeRecord)).toBe("vwap_exit v2");
    expect(exitLabel({ ...base, exit_reason: "TRAILING_STOP", exit_strategy_id: null } as TradeRecord)).toBe("TRAILING_STOP");
  });
  it("contextQuery preserves only source and run", () => {
    expect(contextQuery(new URLSearchParams("source=backtest&run=r1&x=1"))).toBe("?source=backtest&run=r1");
    expect(contextQuery(new URLSearchParams(""))).toBe("");
  });
});
