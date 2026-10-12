"""PAPER / LIVE strategy execution, protection and broker reconciliation.

This package completes the Alpaca runtime that :mod:`mike_trade.app` composes for PAPER and
LIVE.  It reuses the engine's existing pieces instead of building a second engine:

* strategy evaluation, arbitration, persistence of strategy state, Michelle protection facts
  and the 1m shared market context come from :class:`mike_trade.app.VwapReferenceLifecycle`;
* durable accounting goes through the typed repositories (intent -> order -> fill -> cycle);
* discretionary exits, end-of-day closes, safety exits and protection re-establishment run
  through :class:`mike_trade.execution.close_coordinator.CloseCoordinator`;
* broker-native stop ratchets use the confirmation-first
  :class:`mike_trade.execution.replace.StopReplacementCoordinator`.

Broker positions and executions are authoritative; the ledger records them, never the reverse.
"""
