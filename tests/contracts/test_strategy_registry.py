import ast
from dataclasses import FrozenInstanceError, replace
from datetime import UTC, datetime, time, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest

import mike_trade.strategies.base as base_module
import mike_trade.strategies.registry as registry_module
from mike_trade.domain.strategy_types import StrategyRole
from mike_trade.market_data.candles import CandleTimeframe
from mike_trade.strategies.base import (
    BuyStrategy,
    ParameterKind,
    ParameterSchema,
    ParameterSpec,
    ParameterValidationError,
    ReasonCode,
    SellStrategy,
    StrategyEvaluation,
    StrategySpec,
    TriggerKind,
    make_signal_id,
)
from mike_trade.strategies.registry import StrategyRegistry, StrategyRegistryError

BAR_TIME = datetime(2026, 10, 8, 14, 30, tzinfo=UTC)

ALL_REASONS = (
    ReasonCode.SIGNAL,
    ReasonCode.NO_SIGNAL,
    ReasonCode.INSUFFICIENT_DATA,
    ReasonCode.STALE_DATA,
    ReasonCode.NOT_ELIGIBLE,
)


def schema() -> ParameterSchema:
    return ParameterSchema(
        (
            ParameterSpec("lookback", ParameterKind.INT, min_value=2, max_value=50),
            ParameterSpec(
                "threshold",
                ParameterKind.DECIMAL,
                default=Decimal("1.5"),
                min_value=Decimal("0"),
                max_value=Decimal("10"),
            ),
            ParameterSpec("use_volume", ParameterKind.BOOL, default=False),
            ParameterSpec(
                "mode", ParameterKind.ENUM, default="strict", choices=("strict", "loose")
            ),
            ParameterSpec(
                "after",
                ParameterKind.TIME_OF_DAY,
                default=time(9, 45),
                min_value=time(9, 30),
                max_value=time(15, 59),
            ),
        )
    )


def make_spec(
    strategy_id: str = "demo_buy", version: str = "1.0.0", side: StrategyRole = StrategyRole.BUY
) -> StrategySpec:
    return StrategySpec(
        strategy_id=strategy_id,
        version=version,
        side=side,
        reference_symbols=("SPY",),
        required_timeframes=(CandleTimeframe.M5, CandleTimeframe.M15),
        warmup_bars=20,
        completed_bars_only=True,
        requires_volume=True,
        trigger_kind=TriggerKind.EDGE_ON_CLOSED_BAR,
        trigger_timeframe=CandleTimeframe.M5,
        parameter_schema=schema(),
        persistent_state_keys=("armed",),
        methodology="Demo methodology",
        source="unit test",
        execution_limitations=("Long-only; no short entries",),
        reason_codes=ALL_REASONS,
    )


def make_strategy(spec: StrategySpec) -> type:
    base = BuyStrategy if spec.side is StrategyRole.BUY else SellStrategy

    class Demo(base):  # type: ignore[valid-type, misc]
        def evaluate(self, context, params, state):  # type: ignore[no-untyped-def]
            return StrategyEvaluation.no_signal(spec, "AAPL")

    Demo.spec = spec  # type: ignore[attr-defined]
    return Demo


# ------------------------------------------------------------------------ spec


def test_spec_is_frozen_and_hashable_fields_immutable() -> None:
    spec = make_spec()
    with pytest.raises(FrozenInstanceError):
        spec.version = "2.0.0"  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        spec.warmup_bars = 1  # type: ignore[misc]
    assert isinstance(spec.reference_symbols, tuple)
    assert isinstance(spec.required_timeframes, tuple)
    with pytest.raises(FrozenInstanceError):
        spec.parameter_schema.parameters = ()  # type: ignore[misc]


@pytest.mark.parametrize("version", ["1.0.0", "0.12.3", "10.20.30"])
def test_semver_accepts_major_minor_patch(version: str) -> None:
    assert make_spec(version=version).version == version


@pytest.mark.parametrize(
    "version", ["1", "1.0", "1.0.0.0", "v1.0.0", "01.0.0", "1.0.0-rc1", "1.a.0", "", " ", "-1.0.0"]
)
def test_semver_rejects_malformed_versions(version: str) -> None:
    with pytest.raises(ValueError, match="version"):
        make_spec(version=version)


def test_version_key_orders_numerically_not_lexically() -> None:
    assert make_spec(version="1.10.0").version_key > make_spec(version="1.9.0").version_key


def test_spec_rejects_invalid_fields() -> None:
    spec = make_spec()
    with pytest.raises(ValueError, match="strategy_id"):
        replace(spec, strategy_id="Not Snake")
    with pytest.raises(ValueError, match="side"):
        replace(spec, side="BUY")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="warmup_bars"):
        replace(spec, warmup_bars=-1)
    with pytest.raises(ValueError, match="trigger_timeframe"):
        replace(spec, trigger_timeframe=CandleTimeframe.M1)
    with pytest.raises(ValueError, match="SIGNAL and NO_SIGNAL"):
        replace(spec, reason_codes=(ReasonCode.SIGNAL,))
    with pytest.raises(ValueError, match="methodology"):
        replace(spec, methodology=" ")
    with pytest.raises(ValueError, match="reference_symbols"):
        replace(spec, reference_symbols=("SPY", "spy"))


def test_reason_codes_include_required_members_and_no_confidence() -> None:
    names = {code.name for code in ReasonCode}
    assert {"SIGNAL", "NO_SIGNAL", "INSUFFICIENT_DATA", "STALE_DATA", "NOT_ELIGIBLE"} <= names
    fields = set(StrategyEvaluation.__dataclass_fields__)
    assert not any("confidence" in name or "score" in name for name in fields)
    assert not any("confidence" in name for name in StrategySpec.__dataclass_fields__)


# ------------------------------------------------------------------ parameters


def test_parameters_accept_valid_values_and_apply_defaults() -> None:
    validated = schema().validate({"lookback": 14})
    assert validated == {
        "lookback": 14,
        "threshold": Decimal("1.5"),
        "use_volume": False,
        "mode": "strict",
        "after": time(9, 45),
    }
    with pytest.raises(TypeError):
        validated["lookback"] = 3  # type: ignore[index]


def test_parameters_accept_explicit_values_at_inclusive_bounds() -> None:
    validated = schema().validate(
        {
            "lookback": 50,
            "threshold": Decimal("10"),
            "use_volume": True,
            "mode": "loose",
            "after": time(15, 59),
        }
    )
    assert validated["lookback"] == 50
    assert validated["threshold"] == Decimal("10")
    assert schema().validate({"lookback": 2})["lookback"] == 2


def test_decimal_parameter_accepts_int_and_normalizes_to_decimal() -> None:
    value = schema().validate({"lookback": 5, "threshold": 3})["threshold"]
    assert value == Decimal("3") and isinstance(value, Decimal)


def assert_rejected(values: dict[str, object], code: str, parameter: str) -> None:
    with pytest.raises(ParameterValidationError) as excinfo:
        schema().validate(values)
    assert excinfo.value.code == code
    assert excinfo.value.parameter == parameter
    assert isinstance(excinfo.value, ValueError)


def test_parameters_reject_unknown_names() -> None:
    assert_rejected({"lookback": 5, "bogus": 1}, "unknown_parameter", "bogus")


def test_parameters_reject_missing_required() -> None:
    assert_rejected({}, "missing_parameter", "lookback")


@pytest.mark.parametrize(
    ("values", "parameter"),
    [
        ({"lookback": 1}, "lookback"),
        ({"lookback": 51}, "lookback"),
        ({"lookback": 5, "threshold": Decimal("-0.01")}, "threshold"),
        ({"lookback": 5, "threshold": Decimal("10.01")}, "threshold"),
        ({"lookback": 5, "after": time(9, 29)}, "after"),
        ({"lookback": 5, "after": time(16, 0)}, "after"),
    ],
)
def test_parameters_reject_out_of_range(values: dict[str, object], parameter: str) -> None:
    assert_rejected(values, "out_of_range", parameter)


@pytest.mark.parametrize(
    ("values", "parameter"),
    [
        ({"lookback": "5"}, "lookback"),
        ({"lookback": True}, "lookback"),
        ({"lookback": 5, "use_volume": 1}, "use_volume"),
        ({"lookback": 5, "mode": 3}, "mode"),
        ({"lookback": 5, "after": "09:45"}, "after"),
        ({"lookback": 5, "after": time(9, 45, tzinfo=UTC)}, "after"),
        ({"lookback": 5, "threshold": "1.5"}, "threshold"),
        ({"lookback": 5, "threshold": True}, "threshold"),
    ],
)
def test_parameters_reject_wrong_types(values: dict[str, object], parameter: str) -> None:
    assert_rejected(values, "wrong_type", parameter)


@pytest.mark.parametrize("value", [1.5, 2.0, float("nan"), float("inf")])
def test_float_is_rejected_for_decimal_and_int_parameters(value: float) -> None:
    assert_rejected({"lookback": 5, "threshold": value}, "wrong_type", "threshold")
    assert_rejected({"lookback": value}, "wrong_type", "lookback")


def test_decimal_parameter_rejects_non_finite_decimal() -> None:
    assert_rejected({"lookback": 5, "threshold": Decimal("NaN")}, "out_of_range", "threshold")


def test_enum_rejects_value_outside_choices() -> None:
    assert_rejected({"lookback": 5, "mode": "other"}, "invalid_choice", "mode")


def test_non_mapping_parameters_rejected() -> None:
    with pytest.raises(ParameterValidationError) as excinfo:
        schema().validate([("lookback", 5)])  # type: ignore[arg-type]
    assert excinfo.value.code == "wrong_type"


def test_parameter_spec_construction_rules() -> None:
    with pytest.raises(ParameterValidationError):  # float default for Decimal
        ParameterSpec("price_limit", ParameterKind.DECIMAL, default=1.5)
    with pytest.raises(ParameterValidationError):  # float bound for Decimal
        ParameterSpec("price_limit", ParameterKind.DECIMAL, max_value=2.5)
    with pytest.raises(ParameterValidationError):  # min > max
        ParameterSpec("n", ParameterKind.INT, min_value=5, max_value=4)
    with pytest.raises(ParameterValidationError):  # default out of range
        ParameterSpec("n", ParameterKind.INT, default=99, max_value=10)
    with pytest.raises(ParameterValidationError):  # ENUM needs choices
        ParameterSpec("m", ParameterKind.ENUM)
    with pytest.raises(ParameterValidationError):  # choices only for ENUM
        ParameterSpec("n", ParameterKind.INT, choices=("a",))
    with pytest.raises(ParameterValidationError):  # no ranges on bool
        ParameterSpec("flag", ParameterKind.BOOL, min_value=False)
    with pytest.raises(ParameterValidationError):  # bad name
        ParameterSpec("Bad Name", ParameterKind.INT)
    with pytest.raises(ValueError, match="unique"):
        ParameterSchema(
            (ParameterSpec("n", ParameterKind.INT), ParameterSpec("n", ParameterKind.INT))
        )


def test_required_flag_follows_default() -> None:
    assert ParameterSpec("n", ParameterKind.INT).required
    assert not ParameterSpec("n", ParameterKind.INT, default=3).required


# -------------------------------------------------------------------- registry


def test_register_and_lookup_latest_and_pinned() -> None:
    registry = StrategyRegistry()
    v1 = make_strategy(make_spec(version="1.0.0"))
    v2 = make_strategy(make_spec(version="1.2.0"))
    v10 = make_strategy(make_spec(version="1.10.0"))
    for cls in (v10, v1, v2):
        assert registry.register(cls) is cls

    assert registry.get("demo_buy") is v10
    assert registry.get("demo_buy", "1.0.0") is v1
    assert registry.get("demo_buy", version="1.2.0") is v2


def test_lookup_missing_raises_typed_error() -> None:
    registry = StrategyRegistry()
    registry.register(make_strategy(make_spec()))
    with pytest.raises(StrategyRegistryError) as unknown_id:
        registry.get("nope")
    assert unknown_id.value.code == "not_found"
    with pytest.raises(StrategyRegistryError) as unknown_version:
        registry.get("demo_buy", "9.9.9")
    assert unknown_version.value.code == "not_found"


def test_duplicate_registration_is_rejected() -> None:
    registry = StrategyRegistry()
    registry.register(make_strategy(make_spec()))
    with pytest.raises(StrategyRegistryError) as excinfo:
        registry.register(make_strategy(make_spec()))
    assert excinfo.value.code == "duplicate_registration"
    registry.register(make_strategy(make_spec(version="1.0.1")))  # different version is fine


def test_duplicate_key_is_rejected_across_sides() -> None:
    registry = StrategyRegistry()
    registry.register(make_strategy(make_spec("shared_id")))
    with pytest.raises(StrategyRegistryError) as excinfo:
        registry.register(make_strategy(make_spec("shared_id", side=StrategyRole.SELL)))
    assert excinfo.value.code == "duplicate_registration"


def test_list_filters_by_side_and_buy_needs_no_sell_partner() -> None:
    registry = StrategyRegistry()
    assert registry.list_strategies() == ()
    buy = make_strategy(make_spec("alpha_buy"))
    registry.register(buy)  # no SELL partner registered
    assert registry.list_strategies(StrategyRole.BUY) == (buy,)
    assert registry.list_strategies(StrategyRole.SELL) == ()

    sell = make_strategy(make_spec("beta_sell", side=StrategyRole.SELL))
    registry.register(sell)
    assert registry.list_strategies(StrategyRole.SELL) == (sell,)
    assert registry.list_strategies(StrategyRole.BUY) == (buy,)
    assert registry.list_strategies() == (buy, sell)
    with pytest.raises(ValueError):
        registry.list_strategies("BUY")  # type: ignore[arg-type]


def test_registries_are_independent_instances() -> None:
    first, second = StrategyRegistry(), StrategyRegistry()
    first.register(make_strategy(make_spec()))
    assert second.list_strategies() == ()


def test_register_rejects_invalid_classes() -> None:
    registry = StrategyRegistry()

    class NotAStrategy:
        spec = make_spec()

    class Abstract(BuyStrategy):
        spec = make_spec("abstract_buy")

    class NoSpec(BuyStrategy):
        def evaluate(self, context, params, state):  # type: ignore[no-untyped-def]
            raise NotImplementedError

    for candidate in (NotAStrategy, Abstract, NoSpec, "demo_buy", None):
        with pytest.raises(StrategyRegistryError) as excinfo:
            registry.register(candidate)  # type: ignore[arg-type]
        assert excinfo.value.code == "invalid_strategy"
    assert registry.list_strategies() == ()


def test_strategy_class_rejects_spec_of_wrong_side() -> None:
    with pytest.raises(TypeError, match="expected BUY"):

        class WrongSide(BuyStrategy):
            spec = make_spec(side=StrategyRole.SELL)

            def evaluate(self, context, params, state):  # type: ignore[no-untyped-def]
                raise NotImplementedError


def test_parameter_validation_is_delegated_to_spec_schema() -> None:
    registry = StrategyRegistry()
    registry.register(make_strategy(make_spec()))
    assert registry.validate_parameters("demo_buy", {"lookback": 7})["lookback"] == 7
    with pytest.raises(ParameterValidationError) as excinfo:
        registry.validate_parameters("demo_buy", {"lookback": 7, "x": 1}, version="1.0.0")
    assert excinfo.value.code == "unknown_parameter"


def test_registered_strategy_evaluates_to_no_signal_with_reason_codes() -> None:
    cls = make_strategy(make_spec())
    result = cls().evaluate(None, {}, {})
    assert not result.is_signal
    assert result.signal_id is None
    assert result.reason_codes == (ReasonCode.NO_SIGNAL,)


# ----------------------------------------------------------------- signal id


def test_signal_id_is_deterministic_and_hex_sha256() -> None:
    first = make_signal_id("demo_buy", "1.0.0", "AAPL", StrategyRole.BUY, BAR_TIME)
    second = make_signal_id("demo_buy", "1.0.0", "aapl", StrategyRole.BUY, BAR_TIME)
    assert first == second
    assert len(first) == 64 and int(first, 16) >= 0


def test_signal_id_normalizes_equivalent_utc_instants() -> None:
    eastern = timezone(timedelta(hours=-4))
    same_instant = BAR_TIME.astimezone(eastern)
    assert make_signal_id("demo_buy", "1.0.0", "AAPL", StrategyRole.BUY, same_instant) == (
        make_signal_id("demo_buy", "1.0.0", "AAPL", StrategyRole.BUY, BAR_TIME)
    )


def test_signal_id_changes_with_each_identity_field() -> None:
    base = ("demo_buy", "1.0.0", "AAPL", StrategyRole.BUY, BAR_TIME)
    baseline = make_signal_id(*base)
    variants = [
        ("other_buy", "1.0.0", "AAPL", StrategyRole.BUY, BAR_TIME),
        ("demo_buy", "1.0.1", "AAPL", StrategyRole.BUY, BAR_TIME),
        ("demo_buy", "1.0.0", "MSFT", StrategyRole.BUY, BAR_TIME),
        ("demo_buy", "1.0.0", "AAPL", StrategyRole.SELL, BAR_TIME),
        ("demo_buy", "1.0.0", "AAPL", StrategyRole.BUY, BAR_TIME + timedelta(minutes=5)),
    ]
    ids = {make_signal_id(*variant) for variant in variants}
    assert baseline not in ids and len(ids) == len(variants)


def test_signal_id_rejects_naive_time_and_bad_inputs() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        make_signal_id("demo_buy", "1.0.0", "AAPL", StrategyRole.BUY, datetime(2026, 10, 8, 14, 30))
    with pytest.raises(ValueError, match="version"):
        make_signal_id("demo_buy", "1.0", "AAPL", StrategyRole.BUY, BAR_TIME)
    with pytest.raises(ValueError, match="side"):
        make_signal_id("demo_buy", "1.0.0", "AAPL", "BUY", BAR_TIME)  # type: ignore[arg-type]


def test_signal_evaluation_carries_deterministic_id_and_no_confidence() -> None:
    spec = make_spec()
    first = StrategyEvaluation.for_signal(spec, "AAPL", BAR_TIME, reference_price=Decimal("101.25"))
    second = StrategyEvaluation.for_signal(spec, "AAPL", BAR_TIME)
    assert first.is_signal and first.reason_codes == (ReasonCode.SIGNAL,)
    assert first.signal_id == second.signal_id
    assert first.signal_id == make_signal_id(
        "demo_buy", "1.0.0", "AAPL", StrategyRole.BUY, BAR_TIME
    )
    assert not hasattr(first, "confidence")


def test_evaluation_invariants() -> None:
    spec = make_spec()
    with pytest.raises(ValueError, match="signal_id does not match"):
        StrategyEvaluation(
            "demo_buy", "1.0.0", "AAPL", StrategyRole.BUY, (ReasonCode.SIGNAL,),
            bar_time=BAR_TIME, signal_id="0" * 64,
        )  # fmt: skip
    with pytest.raises(ValueError, match="requires a signal_id"):
        StrategyEvaluation(
            "demo_buy", "1.0.0", "AAPL", StrategyRole.BUY, (ReasonCode.SIGNAL,)
        )
    with pytest.raises(ValueError, match="reference_price"):
        StrategyEvaluation.for_signal(spec, "AAPL", BAR_TIME, reference_price=1.5)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="not declared"):
        StrategyEvaluation.no_signal(
            replace(spec, reason_codes=(ReasonCode.SIGNAL, ReasonCode.NO_SIGNAL)),
            "AAPL",
            (ReasonCode.STALE_DATA,),
        )
    no_signal = StrategyEvaluation.no_signal(
        spec, "AAPL", (ReasonCode.INSUFFICIENT_DATA, ReasonCode.NOT_ELIGIBLE)
    )
    assert not no_signal.is_signal
    with pytest.raises(TypeError):
        no_signal.state_updates["armed"] = True  # type: ignore[index]


# ------------------------------------------------------------- static imports

FORBIDDEN_MODULES = {
    # network / HTTP / session clients
    "http", "urllib", "urllib3", "requests", "httpx", "aiohttp", "websocket", "websockets",
    "socket", "ssl", "ftplib", "smtplib", "alpaca", "grpc",
    # filesystem / process / secrets
    "os", "io", "pathlib", "shutil", "tempfile", "glob", "fileinput", "sqlite3", "subprocess",
    "dotenv", "keyring", "secrets",
    # dynamic loading / evaluation
    "importlib", "pkgutil", "runpy", "imp", "pickle", "marshal", "ctypes",
}  # fmt: skip
FORBIDDEN_MIKE_TRADE = (
    "mike_trade.brokers",
    "mike_trade.execution",
    "mike_trade.persistence",
    "mike_trade.runtime",
    "mike_trade.engine",
    "mike_trade.app",
    "mike_trade.cli",
    "mike_trade.config",
    "mike_trade.market_data.alpaca_history",
    "mike_trade.market_data.alpaca_stream",
    "mike_trade.market_data.polling",
    "mike_trade.market_data.service",
)
FORBIDDEN_CALLS = {"eval", "exec", "compile", "open", "__import__"}


def imported_modules(path: Path) -> set[str]:
    modules: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            assert node.level == 0, "relative imports are not allowed"
            modules.add(node.module or "")
            modules.update(f"{node.module}.{alias.name}" for alias in node.names)
    return modules


@pytest.mark.parametrize("module", [base_module, registry_module], ids=["base", "registry"])
def test_modules_do_not_import_broker_network_or_filesystem(module: object) -> None:
    path = Path(module.__file__)  # type: ignore[attr-defined]
    imports = imported_modules(path)
    assert imports, "expected at least one import"
    for name in imports:
        assert name.split(".")[0] not in FORBIDDEN_MODULES, f"{path.name} imports {name}"
        assert not name.startswith(FORBIDDEN_MIKE_TRADE), f"{path.name} imports {name}"


@pytest.mark.parametrize("module", [base_module, registry_module], ids=["base", "registry"])
def test_modules_have_no_dynamic_evaluation_or_file_access(module: object) -> None:
    tree = ast.parse(Path(module.__file__).read_text(encoding="utf-8"))  # type: ignore[attr-defined]
    called = {
        node.func.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    assert not called & FORBIDDEN_CALLS
