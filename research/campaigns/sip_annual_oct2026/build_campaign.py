"""Generate and validate the pinned development and reserved holdout jobs."""

from __future__ import annotations

import json
from collections import Counter
from dataclasses import asdict
from pathlib import Path

from mike_trade.domain.strategy_types import StrategyRole
from mike_trade.research.commands import ExperimentSpec, _research_registry
from mike_trade.risk.stop_config import normalize_stop_spec


HERE = Path(__file__).resolve().parent
START = "2025-10-09T00:00:00Z"
SPLIT = "2026-09-21T04:00:00Z"
END = "2026-10-09T00:00:00Z"
NATIVE = {"type": "native_trailing", "trail_percent": "1.5"}
STEPPED = {"type": "stepped_trailing", "initial_stop_percent": "1.5",
           "step_trigger_percent": "1.0", "step_percent": "0.5"}
FIXED = {"type": "fixed", "stop_percent": "1.0"}


def config(name, category, buy, sell, rationale, *, stop=NATIVE, comparison=None):
    return {"name": name, "category": category, "buy": list(buy), "sell": list(sell),
            "stop": stop, "rationale": rationale, "comparison": comparison}


def configurations():
    out = [
        config("michelle_orb", "michelle", ["five_minute_orb_michelle_buy"],
               ["five_minute_orb_michelle_sell"],
               "Five-minute opening-range break; fill-specific ORB fixed-to-trailing protection.",
               stop=None),
        config("michelle_vwap", "michelle", ["vwap_bounce_break_michelle_buy"],
               ["vwap_bounce_break_michelle_sell"],
               "Michelle VWAP bounce/break; fill-frozen VWAP fixed-to-trailing protection.",
               stop=None),
        config("michelle_combined", "michelle", ["five_minute_orb_michelle_buy",
               "vwap_bounce_break_michelle_buy"], ["five_minute_orb_michelle_sell",
               "vwap_bounce_break_michelle_sell"],
               "Independent ORB and VWAP entries; deterministic winner selects its matching stop.",
               stop=None),
        config("vwap_reference", "reference", ["vwap_bounce"], ["vwap_breakdown"],
               "Original VWAP bounce and breakdown reference with a 1.5% native trail."),
    ]
    controlled = [
        ("trend", ["ema_pullback", "supertrend", "higher_high_higher_low"],
         ["ema_bearish_crossover", "previous_swing_low_break", "dmi_bearish_crossover"],
         "Trend pullback, trend state, and price structure offer separate continuation entries; exits test EMA, structural, and directional weakening."),
        ("breakout", ["opening_range_breakout", "donchian_channel_breakout", "relative_volume_momentum"],
         ["failed_breakout_exit", "momentum_reversal", "high_volume_reversal"],
         "Opening-range, channel, and volume momentum entries capture different expansion events; exits test failed break, momentum fade, and reversal volume."),
        ("vwap_mean", ["vwap_bounce", "vwap_pullback", "rsi_oversold_reversal"],
         ["vwap_breakdown", "rsi_reversal", "price_below_ema"],
         "VWAP support, pullback, and oversold recovery are independent entry opportunities; exits test VWAP loss, oscillator reversal, and trend loss."),
        ("price_action", ["inside_bar_breakout", "support_bounce", "resistance_breakout"],
         ["previous_swing_low_break", "bearish_engulfing_exit", "shooting_star_reversal"],
         "Compression break, support defense, and resistance break are separate price-action entries; exits test structural failure and reversal candles."),
    ]
    for name, buy, sells, rationale in controlled:
        for n in range(4):
            out.append(config(f"{name}_exit_{n}", "controlled_exit", buy, sells[:n],
                              f"{rationale} Controlled exit layer {n}: "
                              + ("protective stop only." if n == 0 else
                                 "technical SELL order " + ", ".join(sells[:n]) + "."),
                              comparison=name))
    extras = [
        ("trend_ema_adaptive", "trend", ["ema_pullback", "adaptive_moving_average"], ["ema_bearish_crossover"], "EMA pullback and adaptive trend turns provide distinct trend entries."),
        ("trend_holy_structure", "trend", ["holy_grail_pullback", "higher_high_higher_low"], ["previous_swing_low_break", "dmi_bearish_crossover"], "ADX-style pullback and rising swing structure offer separate continuation entries."),
        ("trend_super_reclaim", "trend", ["supertrend", "moving_average_reclaim"], ["supertrend_bearish_flip"], "Trend-state turn and average reclaim target separate trend resumption moments."),
        ("trend_dmi_pullback", "trend", ["dmi_crossover", "ema_pullback", "holy_grail_pullback"], ["dmi_bearish_crossover", "price_below_ema"], "Directional turn, EMA pullback, and strong-trend pullback cover different continuation points."),
        ("trend_structure_breakout", "trend", ["higher_high_higher_low", "resistance_breakout", "ema_pullback"], ["previous_swing_low_break", "ema_bearish_crossover"], "Rising structure, resistance break, and EMA pullback are distinct routes into an uptrend."),
        ("momentum_rvol", "momentum", ["momentum_breakout", "relative_volume_momentum"], ["momentum_reversal", "high_volume_reversal"], "Price acceleration and relative-volume expansion provide independent momentum entries."),
        ("momentum_open_flag", "momentum", ["opening_range_breakout", "bull_flag_breakout"], ["failed_breakout_exit", "vwap_breakdown"], "Opening impulse and later flag continuation are different breakout opportunities."),
        ("momentum_squeeze_donchian", "momentum", ["volatility_squeeze_breakout", "donchian_channel_breakout"], ["failed_breakout_exit", "macd_bearish_crossover"], "Volatility release and channel break detect distinct expansion patterns."),
        ("momentum_nr7_volume", "momentum", ["nr7_breakout", "relative_volume_momentum"], ["high_volume_reversal", "momentum_reversal"], "Narrow-range expansion and relative-volume momentum sample compression and participation."),
        ("momentum_flag_drive", "momentum", ["bull_flag_breakout", "opening_drive", "high_of_day_momentum"], ["failed_breakout_exit", "macd_bearish_crossover"], "Early drive, flag continuation, and high-of-day acceleration offer different momentum entries."),
        ("vwap_pullback_band", "vwap_mean", ["vwap_pullback", "vwap_band_reversion"], ["vwap_breakdown", "bollinger_upper_band_reversal"], "VWAP pullback and band reversion sample trend support and statistical stretch."),
        ("vwap_support_oversold", "vwap_mean", ["vwap_bounce", "support_bounce", "rsi_oversold_reversal"], ["vwap_breakdown", "rsi_reversal"], "VWAP defense, chart support, and oscillator recovery are distinct rebound triggers."),
        ("mean_bollinger_rsi", "vwap_mean", ["bollinger_band_mean_reversion", "rsi_oversold_reversal"], ["bollinger_upper_band_reversal", "stochastic_bearish_crossover"], "Band stretch and RSI recovery provide separate mean-reversion entries."),
        ("mean_support_reclaim", "vwap_mean", ["support_bounce", "moving_average_reclaim"], ["price_below_ema", "rsi_reversal"], "Chart support and moving-average reclaim detect different recovery points."),
        ("vwap_retest", "vwap_mean", ["vwap_bounce", "opening_range_retest"], ["vwap_breakdown", "previous_swing_low_break"], "Session VWAP defense and opening-range retest offer separate intraday retest entries."),
        ("price_inside_hammer", "price_action", ["inside_bar_breakout", "hammer_reversal"], ["bearish_engulfing_exit", "shooting_star_reversal"], "Compression release and rejection wick are distinct candle-pattern entries."),
        ("price_double_support", "price_action", ["double_bottom_reversal", "support_bounce"], ["previous_swing_low_break", "lower_low_confirmation"], "Double-bottom recovery and immediate support bounce sample different support behavior."),
        ("price_resistance_retest", "price_action", ["resistance_breakout", "opening_range_retest"], ["failed_breakout_exit", "trendline_breakdown"], "Structural break and opening-range retest provide different price-action entries."),
        ("mixed_trend_breakout", "mixed", ["ema_pullback", "momentum_breakout", "inside_bar_breakout"], ["ema_bearish_crossover", "failed_breakout_exit"], "Trend pullback, momentum expansion, and compression release are independent continuation routes."),
        ("mixed_reclaim_volume", "mixed", ["vwap_pullback", "relative_volume_momentum", "moving_average_reclaim"], ["vwap_breakdown", "high_volume_reversal"], "VWAP pullback, participation surge, and average reclaim sample different renewed-demand signs."),
    ]
    for name, category, buy, sell, rationale in extras:
        out.append(config(name, category, buy, sell, rationale))
    for name, buy, sells, rationale in controlled:
        for stop_name, stop in (("stepped", STEPPED), ("fixed", FIXED)):
            out.append(config(f"{name}_{stop_name}", "stop_comparison", buy, sells[:2],
                              f"{rationale} Same first two technical SELL exits as {name}_exit_2; isolates {stop_name} protection.",
                              stop=stop, comparison=name))
    assert len(out) == 48
    assert len({c["name"] for c in out}) == len(out)
    return out


def build():
    audit = json.loads((HERE / "dataset_audit.json").read_text())
    registry = _research_registry()
    configs = configurations()
    ids = set()
    for c in configs:
        normalize_stop_spec(c["stop"] or NATIVE)
        for role, keys in ((StrategyRole.BUY, c["buy"]), (StrategyRole.SELL, c["sell"])):
            for key in keys:
                cls = registry.get(key)
                if cls.spec.side is not role:
                    raise ValueError(f"{key}: wrong side")
                registry.validate_parameters(key, {})
                ids.add(key)
        if c["category"] == "michelle":
            for buy, sell in (("five_minute_orb_michelle_buy", "five_minute_orb_michelle_sell"),
                              ("vwap_bounce_break_michelle_buy", "vwap_bounce_break_michelle_sell")):
                if buy in c["buy"] and sell not in c["sell"]:
                    raise ValueError(f"{c['name']}: missing Michelle protection {sell}")
    periods = (("experiments.json", START, SPLIT),
               ("holdout_experiments.json", SPLIT, END))
    for filename, left, right in periods:
        jobs = []
        for symbol in ("IBM", "NVDA", "TSLA"):
            for c in configs:
                spec = ExperimentSpec(
                    symbol=symbol, strategy=c["name"], start=left, end=right,
                    dataset_id=audit["symbols"][symbol]["minute_dataset_id"], feed="sip",
                    buy_strategies=tuple(c["buy"]), sell_strategies=tuple(c["sell"]),
                    protective_stop=c["stop"],
                    end_of_day_close={"enabled": True, "at": "15:55"},
                    max_entry_cycles_per_session=10,
                    starting_capital="10000",
                    execution={"slippage_bps": "5", "fee_per_order": "0",
                               "fee_per_share": "0", "fee_bps": "0"},
                )
                spec.validate()
                jobs.append(asdict(spec))
        (HERE / filename).write_text(json.dumps({"experiments": jobs}, indent=2) + "\n")
    manifest = {
        "campaign": "sip_annual_oct2026", "deployed_release": "622ac8815d05d26738c1b81d5b8b68a296519e19",
        "observation_start": START, "observation_end_exclusive": END,
        "development_end_exclusive": SPLIT, "holdout_start": SPLIT,
        "experiments_per_symbol": len(configs), "development_experiments": len(configs) * 3,
        "reserved_holdout_experiments": len(configs) * 3,
        "strategy_count": len(ids), "buy_strategy_count": len({x for c in configs for x in c["buy"]}),
        "sell_strategy_count": len({x for c in configs for x in c["sell"]}),
        "strategy_ids": sorted(ids), "configurations": configs,
        "datasets": {k: {field: value for field, value in row.items() if field != "session_counts"}
                     for k, row in audit["symbols"].items()},
        "assumptions": {"capital_usd": "10000", "slippage_bps": "5", "fees_usd": "0",
                        "feed": "sip", "adjustment": "raw", "timeframe": "1m",
                        "bar_fill_policy": "NEXT_BAR_OPEN", "ambiguity_policy": "STOP_FIRST",
                        "max_entry_cycles_per_session": 10,
                        "end_of_day_close": {"enabled": True, "at": "15:55",
                                             "timezone": "America/New_York"}},
    }
    (HERE / "campaign_manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    rows = ["| Configuration | Category | BUY strategies | SELL strategies | Protection | Rationale |",
            "|---|---|---|---|---|---|"]
    for c in configs:
        stop = "Michelle fill-specific" if c["stop"] is None else c["stop"]["type"]
        rows.append("| " + " | ".join((c["name"], c["category"], ", ".join(c["buy"]),
                                      ", ".join(c["sell"]) or "none",
                                      stop, c["rationale"])) + " |")
    readme = f"""# Annual SIP strategy campaign — October 2026

The campaign pins immutable Alpaca consolidated SIP, RAW snapshots from **{START}** through **{END}** (exclusive). The exploratory development batch ends at **{SPLIT}**. The holdout batch starts at that boundary and must be run only after configurations are selected from development results. No holdout result is used to tune this first batch.

There are **{len(configs)} distinct configurations per symbol**, **{len(configs) * 3} development jobs**, and **{len(ids)} registered strategy identifiers** ({manifest['buy_strategy_count']} BUY, {manifest['sell_strategy_count']} SELL). The same 144 definitions are prepared separately for the reserved holdout. All parameters use current registry defaults, checked by `build_campaign.py`.

## Pinned data and completeness

| Symbol | Annual minute dataset | All minute bars | Regular-session bars | Sessions present | Complete sessions | Missing regular minutes | Daily SIP dataset | Daily bars |
|---|---|---:|---:|---:|---:|---:|---|---:|
"""
    for symbol, row in audit["symbols"].items():
        readme += (f"| {symbol} | `{row['minute_dataset_id']}` | {row['minute_bars']:,} | "
                   f"{row['regular_session_bars']:,} | {row['sessions_with_bars']}/{row['expected_sessions']} | "
                   f"{row['full_regular_sessions']} | {row['missing_regular_minutes']} | "
                   f"`{row['daily_dataset_id']}` | {row['daily_bars']} |\n")
    readme += """
The snapshots include extended-hours observations, but historical replay uses regular-session bars only. IBM has sparse regular minutes on 22 sessions; no candles were filled in. See `dataset_audit.json` for daily counts. The existing full-window snapshots were reused; the original snapshots and acquisition logs were not changed. All minute and daily hashes, coverage, unique timestamps, SIP/RAW provenance, and OHLCV bounds were checked. Daily SIP history remains available for Michelle's ATR(14).

## Design and execution

BUY strategies are independent opportunities. The ordinary deterministic arbitration picks one signal; one filled position owns the symbol until a full exit. SELL strategies also compete independently; the first winning exit closes the position. The 16 controlled-exit configurations hold each BUY collection and native 1.5% trail fixed while varying zero to three technical SELL exits. Eight stop comparisons keep the same BUY and first two SELL strategies while replacing only the generic stop with a 1.0% fixed stop or 1.5% initial, 1.0% trigger, 0.5% step trail.

Michelle ORB and VWAP use their own matching SELL protective policies: ORB high or fill-frozen VWAP minus $0.25 initially, then a 0.5% high-water trail only when `0.995 * highest_price > reference + $0.25` (strict). The combined configuration includes both SELL components; each actual entry selects its matching policy. The standard 1.5% native trail is only the runner's mandatory fallback if Michelle reference facts are unavailable; `summarize_results.py` flags any Michelle cycle that used this fallback. The reference VWAP pair uses the native 1.5% trail.

Capital is $10,000 per independent run. Slippage is 5 bps and primary fees are zero. The existing historical simulator executes completed-bar signals at the next eligible bar open with conservative stop-first OHLC ambiguity. It uses no fabricated intrabar path. The entry-cycle gate is **10 completed entries per symbol per session**, with one position at a time and no pyramiding. The runner's ordinary cutoff is 30 minutes before the session close; Michelle's own 11:30 ET cutoff and shared rolling-attempt restrictions remain active. EOD liquidation starts at **15:55 America/New_York**, or five minutes before an early close, through the existing historical EOD logic. Open positions are not intentionally carried overnight.

## Configurations

""" + "\n".join(rows) + "\n\n" + """## Run and inspect

From the project root, run `research/campaigns/sip_annual_oct2026/run_campaign.sh`. Set `WORKERS=4` (default) or another positive count. This invokes the existing Docker Compose research service and process-pool batch runner, writes to a new timestamped directory under `research/results/sip_annual_oct2026/`, and then builds CSV, JSON, and Markdown summaries. It does not invoke the reserved holdout. The script prints the exact result path; `summary.md` is the concise research report, `summary.csv`/`summary.json` are machine-readable, and `trade_ledgers/` retains complete per-run trades including MFE/MAE. Immutable replay artifacts remain in `executions/`.

After choosing configurations using development results only, run the reserved holdout separately with `research/campaigns/sip_annual_oct2026/run_campaign.sh holdout "config1,config2"`. The script requires the selected configuration names, filters all three symbols into a pinned copy in a new unique results directory, and never runs the full reserved set by default. Inspect per-configuration stop/exit distributions, strategy contribution, active days, and zero-trade cases before comparing P&L; a strategy with no trades is not a loss. The report lists supported maximum drawdown and uses N/A for missing values.
"""
    (HERE / "README.md").write_text(readme)
    print(f"validated {len(configs)} configurations, {len(configs) * 3} jobs per period, {len(ids)} strategy identifiers")


if __name__ == "__main__":
    build()
