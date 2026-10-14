"""Optional discretionary trade review (JEV) between strategy arbitration and order submission.

Submodules (imported explicitly; this package intentionally imports none of them so strategy
modules can depend on :mod:`mike_trade.review.evidence` without cycles):

* :mod:`~mike_trade.review.evidence` - the versioned strategy-evidence contract;
* :mod:`~mike_trade.review.proposal` - trade proposals and the compact reviewer request;
* :mod:`~mike_trade.review.reviewer` - provider-independent reviewer protocol and strict parsing;
* :mod:`~mike_trade.review.openrouter` - the OpenRouter provider (JEV is reached only through it);
* :mod:`~mike_trade.review.stage` - scope, modes, error policy, caching/replay, recording;
* :mod:`~mike_trade.review.store` - append-only review records and the replay/cache index;
* :mod:`~mike_trade.review.config` / :mod:`~mike_trade.review.factory` - configuration;
* :mod:`~mike_trade.review.analysis` - executed-versus-counterfactual outcome reporting.
"""
