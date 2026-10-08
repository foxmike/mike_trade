"""Deterministic, lossless serialization for every domain contract.

Standard library only (``json``, ``dataclasses``, ``enum``, ``decimal``,
``datetime``, ``types``). No pydantic, no Alpaca types, and no imports from
``mike_trade.brokers`` or ``mike_trade.strategies``.

Design choices (3):
1. ``timedelta`` values are encoded as an integer count of total microseconds
   (computed with pure integer arithmetic, never via float) and decoded back
   with ``timedelta(microseconds=n)``.
2. Mapping keys are encoded as strings: ``str`` keys pass through unchanged
   and ``Enum`` keys (e.g. ``Timeframe`` in ``MarketContext.candles``) use the
   member name; union fields are decoded by trying each non-``None``
   candidate annotation in declaration order and taking the first strict
   match.
3. Dataclass payloads must carry exactly the constructor field set: unknown
   fields and missing fields are both rejected, and decoding always runs the
   normal domain constructors so invariants are re-validated on decode.
"""

from __future__ import annotations

import dataclasses
import enum
import hashlib
import json
from collections.abc import Mapping
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal, InvalidOperation
from types import MappingProxyType, UnionType
from typing import Any, Union, get_args, get_origin, get_type_hints

from mike_trade.domain import accounts as _accounts_mod
from mike_trade.domain import instruments as _instruments_mod
from mike_trade.domain import market as _market_mod
from mike_trade.domain import protection as _protection_mod
from mike_trade.domain import signals as _signals_mod
from mike_trade.domain import strategy_types as _strategy_types_mod
from mike_trade.domain import trading as _trading_mod

__all__ = [
    "register",
    "to_primitive",
    "from_primitive",
    "dumps",
    "loads",
    "content_hash",
]

_TAG_TO_CLASS: dict[str, type] = {}
_CLASS_TO_TAG: dict[type, str] = {}
_HINTS_CACHE: dict[type, dict[str, Any]] = {}


def register(cls: type, tag: str | None = None) -> type:
    """Register a frozen dataclass under a stable type tag.

    ``tag`` defaults to the class name. Later phases may call this to add
    new domain types. Conflicting re-registrations raise ``ValueError``.
    """
    if not isinstance(cls, type) or not dataclasses.is_dataclass(cls):
        raise TypeError(f"register() requires a dataclass; got {cls!r}")
    name = cls.__name__ if tag is None else tag
    if not isinstance(name, str) or not name:
        raise ValueError(f"type tag must be a non-empty str; got {name!r}")
    existing = _TAG_TO_CLASS.get(name)
    if existing is not None and existing is not cls:
        raise ValueError(f"type tag {name!r} is already registered")
    existing_tag = _CLASS_TO_TAG.get(cls)
    if existing_tag is not None and existing_tag != name:
        raise ValueError(f"{cls.__name__} is already registered as {existing_tag!r}")
    _TAG_TO_CLASS[name] = cls
    _CLASS_TO_TAG[cls] = name
    return cls


def _register_defaults() -> None:
    for module in (
        _instruments_mod,
        _market_mod,
        _signals_mod,
        _strategy_types_mod,
        _trading_mod,
        _protection_mod,
        _accounts_mod,
    ):
        names = getattr(module, "__all__", None)
        if names is None:
            names = [name for name in dir(module) if not name.startswith("_")]
        for attr_name in names:
            candidate = getattr(module, attr_name, None)
            if isinstance(candidate, type) and dataclasses.is_dataclass(candidate):
                register(candidate)


def _timedelta_to_microseconds(value: timedelta) -> int:
    return (value.days * 86400 + value.seconds) * 1_000_000 + value.microseconds


def _sorted_key(primitive: Any) -> str:
    return json.dumps(primitive, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def to_primitive(obj: Any) -> Any:
    """Convert ``obj`` to JSON-safe primitives with canonical scalar forms."""
    if obj is None or isinstance(obj, (bool, int, str)):
        return obj
    if isinstance(obj, float):
        # Only reachable inside untyped ``object`` mappings (strategy
        # parameters / research); Decimal-typed domain fields never hold
        # floats because their constructors reject them.
        return obj
    if isinstance(obj, Decimal):
        return str(obj)
    if isinstance(obj, datetime):
        if obj.tzinfo is None or obj.utcoffset() is None:
            raise ValueError("naive datetimes cannot be encoded; require tz-aware")
        return obj.astimezone(UTC).isoformat()
    if isinstance(obj, date):
        return obj.isoformat()
    if isinstance(obj, time):
        return obj.isoformat()
    if isinstance(obj, timedelta):
        return _timedelta_to_microseconds(obj)
    if isinstance(obj, enum.Enum):
        return obj.name
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        tag = _CLASS_TO_TAG.get(type(obj))
        if tag is None:
            raise ValueError(f"unregistered dataclass: {type(obj).__name__}")
        encoded: dict[str, Any] = {"__type__": tag}
        for field in dataclasses.fields(obj):
            encoded[field.name] = to_primitive(getattr(obj, field.name))
        return encoded
    if isinstance(obj, (tuple, list)):
        return [to_primitive(item) for item in obj]
    if isinstance(obj, (frozenset, set)):
        items = [to_primitive(item) for item in obj]
        return sorted(items, key=_sorted_key)
    if isinstance(obj, Mapping):
        encoded_map: dict[str, Any] = {}
        for key, value in obj.items():
            if isinstance(key, enum.Enum):
                key_str = key.name
            elif isinstance(key, str):
                key_str = key
            else:
                raise ValueError(f"unsupported mapping key: {key!r}")
            encoded_map[key_str] = to_primitive(value)
        return encoded_map
    raise TypeError(f"unsupported type for serialization: {type(obj).__name__}")


def _namespace() -> dict[str, Any]:
    namespace: dict[str, Any] = {
        "Any": Any,
        "Decimal": Decimal,
        "datetime": datetime,
        "date": date,
        "time": time,
        "timedelta": timedelta,
        "Mapping": Mapping,
    }
    for cls in _CLASS_TO_TAG:
        namespace[cls.__name__] = cls
    for module in (
        _instruments_mod,
        _market_mod,
        _signals_mod,
        _strategy_types_mod,
        _trading_mod,
        _protection_mod,
        _accounts_mod,
    ):
        for attr_name in getattr(module, "__all__", ()):
            candidate = getattr(module, attr_name, None)
            if isinstance(candidate, type):
                namespace.setdefault(candidate.__name__, candidate)
    return namespace


def _hints_for(cls: type) -> dict[str, Any]:
    cached = _HINTS_CACHE.get(cls)
    if cached is not None:
        return cached
    try:
        hints = get_type_hints(cls, globalns=_namespace(), localns=_namespace())
    except Exception:
        hints = {}
        namespace = _namespace()
        for field in dataclasses.fields(cls):
            annotation = field.type
            if isinstance(annotation, str):
                try:
                    annotation = eval(annotation, dict(namespace))  # noqa: S307
                except Exception:
                    annotation = Any
            hints[field.name] = annotation
    _HINTS_CACHE[cls] = hints
    return hints


def _decode_datetime(primitive: Any, *, path: str) -> datetime:
    if not isinstance(primitive, str):
        raise TypeError(f"{path} must be an ISO-8601 datetime string")
    text = primitive.strip()
    candidate = text[:-1] + "+00:00" if text.endswith(("Z", "z")) else text
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError:
        raise ValueError(f"{path} is not a valid ISO-8601 datetime: {primitive!r}") from None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{path} must be timezone-aware; got naive {primitive!r}")
    return parsed.astimezone(UTC)


def _decode_date(primitive: Any, *, path: str) -> date:
    if not isinstance(primitive, str):
        raise TypeError(f"{path} must be an ISO-8601 date string")
    try:
        return date.fromisoformat(primitive.strip())
    except ValueError:
        raise ValueError(f"{path} is not a valid ISO date: {primitive!r}") from None


def _decode_time(primitive: Any, *, path: str) -> time:
    if not isinstance(primitive, str):
        raise TypeError(f"{path} must be an ISO-8601 time string")
    try:
        return time.fromisoformat(primitive.strip())
    except ValueError:
        raise ValueError(f"{path} is not a valid ISO time: {primitive!r}") from None


def _decode_decimal(primitive: Any, *, path: str) -> Decimal:
    if isinstance(primitive, bool) or not isinstance(primitive, str):
        raise TypeError(f"{path} must be a decimal string; got {primitive!r}")
    try:
        result = Decimal(primitive)
    except (InvalidOperation, ValueError, ArithmeticError):
        raise ValueError(f"{path} is not a valid decimal: {primitive!r}") from None
    if not result.is_finite():
        raise ValueError(f"{path} must be finite; got {primitive!r}")
    return result


def _decode_enum(primitive: Any, annotation: type[enum.Enum], *, path: str) -> enum.Enum:
    if not isinstance(primitive, str):
        raise TypeError(f"{path} must be an enum member name; got {primitive!r}")
    try:
        return annotation[primitive]
    except KeyError:
        raise ValueError(
            f"{path} is not a member of {annotation.__name__}: {primitive!r}"
        ) from None


def _decode_union(primitive: Any, args: tuple[Any, ...], *, path: str) -> Any:
    if primitive is None and type(None) in args:
        return None
    errors: list[str] = []
    for candidate in args:
        if candidate is type(None):
            continue
        try:
            return _decode_for_annotation(primitive, candidate, path=path)
        except (TypeError, ValueError) as exc:
            errors.append(str(exc))
            continue
    raise ValueError(f"{path} matches no union candidate: {primitive!r} ({'; '.join(errors)})")


def _decode_variadic(primitive: Any, item_annotation: Any, *, path: str, make: Any) -> Any:
    if not isinstance(primitive, list):
        raise TypeError(f"{path} must be a JSON array; got {type(primitive).__name__}")
    return make(
        _decode_for_annotation(item, item_annotation, path=f"{path}[{index}]")
        for index, item in enumerate(primitive)
    )


def _decode_sequence(
    primitive: Any, args: tuple[Any, ...], *, path: str, make: Any
) -> Any:
    if not isinstance(primitive, list):
        raise TypeError(f"{path} must be a JSON array; got {type(primitive).__name__}")
    if not args:
        return make(_decode_generic(item, path=f"{path}[{index}]") for item in primitive)
    if len(args) == 2 and args[1] is Ellipsis:
        return make(
            _decode_for_annotation(item, args[0], path=f"{path}[{index}]")
            for index, item in enumerate(primitive)
        )
    if len(args) == len(primitive):
        return make(
            _decode_for_annotation(item, annotation, path=f"{path}[{index}]")
            for index, (item, annotation) in enumerate(zip(primitive, args))
        )
    raise ValueError(
        f"{path} has {len(primitive)} items but expects a {len(args)}-tuple"
    )


def _decode_mapping(primitive: Any, args: tuple[Any, ...], *, path: str) -> dict[Any, Any]:
    if not isinstance(primitive, dict):
        raise TypeError(f"{path} must be a JSON object; got {type(primitive).__name__}")
    key_annotation, value_annotation = (args + (Any, Any))[:2]
    decoded: dict[Any, Any] = {}
    for raw_key, raw_value in primitive.items():
        if isinstance(key_annotation, type) and issubclass(key_annotation, enum.Enum):
            new_key: Any = _decode_enum(raw_key, key_annotation, path=f"{path}.key")
        elif key_annotation is str or key_annotation is Any or key_annotation is object:
            if not isinstance(raw_key, str):
                raise TypeError(f"{path}.key must be a string; got {raw_key!r}")
            new_key = raw_key
        else:
            new_key = _decode_for_annotation(raw_key, key_annotation, path=f"{path}.key")
        decoded[new_key] = _decode_for_annotation(
            raw_value, value_annotation, path=f"{path}.{raw_key}"
        )
    return decoded


def _resolve_annotation(annotation: Any, *, path: str) -> Any:
    if isinstance(annotation, str):
        namespace = _namespace()
        if annotation in namespace:
            return namespace[annotation]
        try:
            return eval(annotation, dict(namespace))  # noqa: S307
        except Exception:
            raise ValueError(f"{path} has an unresolvable annotation: {annotation!r}") from None
    return annotation


def _decode_for_annotation(primitive: Any, annotation: Any, *, path: str) -> Any:
    annotation = _resolve_annotation(annotation, path=path)
    if annotation is None or annotation is Any or annotation is object:
        return _decode_generic(primitive, path=path)
    if annotation is type(None):
        if primitive is not None:
            raise TypeError(f"{path} must be null; got {primitive!r}")
        return None
    origin = get_origin(annotation)
    if origin is Union or (isinstance(annotation, UnionType)):
        return _decode_union(primitive, get_args(annotation), path=path)
    if origin is Union or origin is UnionType:
        return _decode_union(primitive, get_args(annotation), path=path)
    if annotation is Decimal:
        return _decode_decimal(primitive, path=path)
    if annotation is bool:
        if not isinstance(primitive, bool):
            raise TypeError(f"{path} must be a bool; got {primitive!r}")
        return primitive
    if annotation is int:
        if isinstance(primitive, bool) or not isinstance(primitive, int):
            raise TypeError(f"{path} must be an int; got {primitive!r}")
        return primitive
    if annotation is str:
        if not isinstance(primitive, str):
            raise TypeError(f"{path} must be a string; got {primitive!r}")
        return primitive
    if annotation is float:
        if isinstance(primitive, bool) or not isinstance(primitive, (int, float)):
            raise TypeError(f"{path} must be a number; got {primitive!r}")
        return float(primitive)
    if annotation is datetime:
        return _decode_datetime(primitive, path=path)
    if annotation is date:
        return _decode_date(primitive, path=path)
    if annotation is time:
        return _decode_time(primitive, path=path)
    if annotation is timedelta:
        if isinstance(primitive, bool) or not isinstance(primitive, int):
            raise TypeError(f"{path} must be integer microseconds; got {primitive!r}")
        return timedelta(microseconds=primitive)
    if isinstance(annotation, type) and issubclass(annotation, enum.Enum):
        return _decode_enum(primitive, annotation, path=path)
    if isinstance(annotation, type) and dataclasses.is_dataclass(annotation):
        if not isinstance(primitive, dict):
            raise TypeError(f"{path} must be an object for {annotation.__name__}")
        return _decode_dataclass(primitive, annotation, path=path)
    if origin is tuple:
        return _decode_sequence(primitive, get_args(annotation), path=path, make=tuple)
    if origin is list:
        args = get_args(annotation)
        item_annotation: Any = args[0] if len(args) == 1 else Any
        return _decode_variadic(primitive, item_annotation, path=path, make=list)
    if origin in (frozenset, set):
        args = get_args(annotation)
        item_annotation = args[0] if len(args) == 1 else Any
        items = _decode_variadic(primitive, item_annotation, path=path, make=list)
        return frozenset(items)
    if origin is not None:
        try:
            if isinstance(origin, type) and issubclass(origin, Mapping):
                return _decode_mapping(primitive, get_args(annotation), path=path)
        except TypeError:
            pass
        if origin is dict:
            return _decode_mapping(primitive, get_args(annotation), path=path)
    if annotation is MappingProxyType:
        if not isinstance(primitive, dict):
            raise TypeError(f"{path} must be a JSON object")
        return MappingProxyType(dict(primitive))
    raise ValueError(f"{path} has an unsupported annotation: {annotation!r}")


def _decode_generic(primitive: Any, *, path: str) -> Any:
    if isinstance(primitive, dict):
        tag = primitive.get("__type__")
        if tag is not None:
            if not isinstance(tag, str):
                raise TypeError(f"{path}.__type__ must be a string")
            cls = _TAG_TO_CLASS.get(tag)
            if cls is None:
                raise ValueError(f"unknown __type__: {tag!r}")
            return _decode_dataclass(primitive, cls, path=path)
        return {
            key: _decode_generic(value, path=f"{path}.{key}")
            for key, value in primitive.items()
        }
    if isinstance(primitive, list):
        return [
            _decode_generic(item, path=f"{path}[{index}]")
            for index, item in enumerate(primitive)
        ]
    return primitive


def _decode_dataclass(data: dict[str, Any], cls: type, *, path: str) -> Any:
    if not isinstance(data, dict):
        raise TypeError(f"{path} must be a JSON object for {cls.__name__}")
    tag = data.get("__type__")
    expected_tag = _CLASS_TO_TAG.get(cls)
    if tag is not None and tag != expected_tag:
        raise ValueError(f"{path} tag {tag!r} does not match {expected_tag!r}")
    field_names = {field.name for field in dataclasses.fields(cls)}
    keys = set(data.keys()) - {"__type__"}
    extra = sorted(keys - field_names)
    if extra:
        raise ValueError(f"{path} has unknown fields for {cls.__name__}: {extra}")
    missing = sorted(field_names - keys)
    if missing:
        raise ValueError(f"{path} is missing fields for {cls.__name__}: {missing}")
    hints = _hints_for(cls)
    kwargs: dict[str, Any] = {}
    for field in dataclasses.fields(cls):
        annotation = hints.get(field.name, Any)
        kwargs[field.name] = _decode_for_annotation(
            data[field.name], annotation, path=f"{path}.{field.name}"
        )
    return cls(**kwargs)


def from_primitive(data: Any, expected_type: Any | None = None) -> Any:
    """Reconstruct domain instances from JSON-safe primitives.

    Runs the normal domain constructors so invariants are re-validated.
    Rejects unknown tags, unknown/missing fields, floats for ``Decimal``
    fields, and naive datetime strings.
    """
    if expected_type is not None:
        return _decode_for_annotation(data, expected_type, path="$")
    return _decode_generic(data, path="$")


def dumps(obj: Any) -> str:
    """Encode ``obj`` as canonical JSON (sorted keys, compact separators)."""
    return json.dumps(to_primitive(obj), sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def loads(text: str) -> Any:
    """Decode canonical JSON produced by :func:`dumps`."""
    return from_primitive(json.loads(text))


def content_hash(obj: Any) -> str:
    """Return the SHA-256 hex digest of :func:`dumps` for ``obj``."""
    return hashlib.sha256(dumps(obj).encode("utf-8")).hexdigest()


_register_defaults()
