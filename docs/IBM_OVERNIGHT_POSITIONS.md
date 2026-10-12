# IBM overnight position audit

The October 12 audit rates are consistent with the research runner's actual
configuration. `research_backtest` always adds the 30-minute entry cutoff, the
one-entry-per-session cap, and a 60-second strategy-exit hold, but it does not
set `end_of_day_close`. The runner and example configuration default EOD close
to disabled. Therefore those IBM runs did not request an EOD liquidation.

The different carry rates follow from the exit rules: stop-only cycles had no
technical SELL exit and remained open whenever the protective stop was not
triggered; EMA SELL and VWAP SELL closed more positions through their configured
signals; the combined SELL collection closed nearly all cycles. A protective
stop is a conditional order, not a session close, and only executes when its
trigger is reached. The research command defaults to a 1.5% native trailing
stop and the runner arms it after the entry fill; its `INITIAL_STOP_ESTABLISHED`
event records that establishment. The stop is GTC and survives session
boundaries. Strategy state and open cycles also survive session boundaries in a
single replay, so with EOD disabled a position can remain open into the next
session. Missing IEX bars do not cause a stop fill by themselves.

There was a backtest code defect for explicitly enabled EOD liquidation: the
runner silently ignored `end_of_day_close.enabled: true`. The runner now submits
a DAY market close at the configured New York time (default 15:55, adjusted to
five minutes before an early close). The protective order is canceled before
the exit is submitted, consistent with broker order handling. The market order
fills only against a later available quote or regular-session bar. If final
bars and quotes are missing, there is no defensible simulated fill; the DAY
order can remain unfilled and expire, and the cycle remains open in the
artifact. Thus missing IEX data can prevent a *simulated* liquidation; it is
not replaced with a fabricated close-price fill. In live execution, broker
acknowledgment and actual fill reports determine whether the close succeeded.

For intraday-only IBM research jobs, add this field to each job:

```json
{"end_of_day_close": {"enabled": true, "at": "15:55"}}
```

The research runner already sets `entry_cutoff_minutes: 30`,
`max_entry_cycles_per_session: 1`, and `minimum_hold_seconds: 60`. The job's
`end_of_day_close` field is now passed into the run configuration and manifest.
The effective intraday configuration is therefore:

```json
{
  "entry_cutoff_minutes": 30,
  "max_entry_cycles_per_session": 1,
  "minimum_hold_seconds": 60,
  "end_of_day_close": {"enabled": true, "at": "15:55"}
}
```

This keeps the intentional one-entry cap at one and adds EOD liquidation.
