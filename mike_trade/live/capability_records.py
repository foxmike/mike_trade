"""Operator-recorded Alpaca order-capability evidence (fail closed).

The default capability matrix proves only whole-share market/limit orders and standalone
trailing stops.  Michelle's fixed-to-trailing protection additionally needs a standalone
``stop`` SELL and a ``stop_price`` replacement, which stay UNPROVEN until an operator records
the evidence.  Evidence lives in ``<data_dir>/alpaca_capabilities.json`` so it is explicit,
reviewable and never inferred:

.. code-block:: json

    {"records": [
      {"environment": "PAPER", "kind": "paper_verification", "reference": "PV-2026-10-12-1",
       "recorded_on": "2026-10-12", "order_type": "stop", "sizing": "whole",
       "time_in_force": "gtc", "side": "sell", "operation": "submit"},
      {"environment": "PAPER", "kind": "paper_verification", "reference": "PV-2026-10-12-2",
       "recorded_on": "2026-10-12", "order_type": "stop", "sizing": "whole",
       "time_in_force": "gtc", "side": "sell", "operation": "replace"}
    ]}

A PAPER entry must be a ``paper_verification`` and promotes only PAPER; a LIVE entry must be an
``operator_record`` and promotes only LIVE.  Paper evidence never promotes LIVE.  A malformed
file raises (the engine refuses to start) rather than silently ignoring evidence.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from mike_trade.brokers.alpaca.capabilities import (
    BrokerCapabilities,
    CapabilityDescriptor,
    CapabilityEvidence,
    default_alpaca_capabilities,
    record_paper_verification,
)
from mike_trade.domain.errors import ConfigurationError

__all__ = ["load_capability_matrix"]


def load_capability_matrix(path: str | Path | None) -> BrokerCapabilities:
    matrix = default_alpaca_capabilities()
    if path is None:
        return matrix
    file = Path(path)
    if not file.is_file():
        return matrix
    try:
        document: Any = json.loads(file.read_text(encoding="utf-8"))
        records = document["records"]
        for record in records:
            descriptor = CapabilityDescriptor(
                order_type=record["order_type"],
                order_class=record.get("order_class", "simple"),
                sizing=record.get("sizing", "whole"),
                time_in_force=record["time_in_force"],
                side=record["side"],
                operation=record["operation"],
            )
            environment = str(record["environment"]).upper()
            kind = str(record["kind"]).lower()
            if environment == "PAPER" and kind == "paper_verification":
                matrix = record_paper_verification(
                    matrix, descriptor, str(record["reference"]), str(record["recorded_on"]),
                    str(record.get("note", "")),
                )
            elif environment == "LIVE" and kind == "operator_record":
                matrix = matrix.with_live_promotion(
                    descriptor,
                    CapabilityEvidence(
                        "operator_record", str(record["reference"]), str(record["recorded_on"]),
                        str(record.get("note", "")),
                    ),
                )
            else:
                raise ValueError(
                    f"{environment} entries must be {'paper_verification' if environment == 'PAPER' else 'operator_record'}"
                )
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise ConfigurationError(
            f"invalid Alpaca capability records in {file}: {exc}", code="CAPABILITY_RECORDS_INVALID"
        ) from exc
    return matrix
