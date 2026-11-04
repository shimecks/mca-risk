"""Generic column-mapping engine: ``mappings/*.yaml`` (data) + :mod:`mcarisk.parse` (generic parsers).

A mapping names its ``source_table`` and ``target`` and lists canonical ``fields``. Each field is one of

* ``{column: <raw header>, parser: <name>, ...options}`` -- parse that raw cell;
* ``{from_hints: [<field>, ...], default: <value>}`` -- take a hint another parser produced
  (``"$2,812.14 wkly"`` and ``"24 wks"`` both hint ``weekly``); conflicting hints are a failure;

plus optional ``derivations`` (fill a field the source lacks) and ``repairs`` (fix a lossy value), each
a named rule implemented here once. Every non-trivial event is returned as a :class:`LogEntry` so the
stage can write it to ``std_log`` (METRICS.md Q7: loaded, rejected with a reason, or repaired with a log
entry). Parser options:

``required``      a failure or missing value rejects the whole row (default false: field -> NULL + log)
``allow_negative`` money/int may be negative (ACH amounts are signed)
``unit``          rate unit for bare numbers: ``percent`` | ``fraction``
``max_fraction``  rate upper bound (default 1)
``table``         lookup table name for ``parser: lookup`` (``mappings/<table>.yaml``)
``allowed``       closed set of values for ``parser: code``
``format_to``     for ``parser: date``: also store the matched date format in this field
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any

import yaml

from mcarisk import parse as P
from mcarisk import paths

LOOKUP_TABLES = ("industry", "status", "collection_events")

ACTION_REJECTED = "rejected"
ACTION_NULLED = "nulled"  # optional field unparseable -> NULL
ACTION_REPAIRED = "repaired"  # value changed from what the text literally says
ACTION_DERIVED = "derived"  # field absent from the source, computed by a named rule


@dataclass(frozen=True)
class LogEntry:
    field: str
    action: str
    reason: str
    raw_value: str | None
    value: str | None = None


@dataclass
class Mapping:
    name: str
    source_table: str
    target: str
    fields: dict[str, dict[str, Any]]
    source_family: str | None = None
    derivations: dict[str, str] = field(default_factory=dict)
    repairs: list[str] = field(default_factory=list)
    checks: dict[str, Any] = field(default_factory=dict)
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def columns(self) -> list[str]:
        """Raw columns this mapping reads (must all exist in the source table)."""
        return sorted({s["column"] for s in self.fields.values() if "column" in s})


@dataclass
class RowResult:
    values: dict[str, Any]
    logs: list[LogEntry]
    reject: LogEntry | None = None  # set when a required field failed


def load_yaml(path: Path) -> Any:
    with open(path, encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def load_lookups(mappings_dir: Path = paths.MAPPINGS_DIR) -> dict[str, dict[str, str]]:
    """{table: {normalized variant: canonical}}. A variant mapped to two canonicals is an error."""
    out: dict[str, dict[str, str]] = {}
    for name in LOOKUP_TABLES:
        data = load_yaml(mappings_dir / f"{name}.yaml") or {}
        table: dict[str, str] = {}
        for canonical, variants in data.items():
            for v in [canonical, *variants] if name == "industry" else variants:
                k = P.norm_lookup_key(str(v))
                if table.get(k, canonical) != canonical:
                    raise ValueError(f"{name}.yaml: {v!r} maps to both {table[k]!r} and {canonical!r}")
                table[k] = str(canonical)
        out[name] = table
    return out


_KNOWN_KEYS = {"source_table", "target", "source_family", "fields", "derivations", "repairs", "checks"}


def load_mapping(name: str, mappings_dir: Path = paths.MAPPINGS_DIR) -> Mapping:
    data = load_yaml(mappings_dir / f"{name}.yaml")
    for fname, spec in data["fields"].items():
        if "column" in spec and spec.get("parser") not in PARSERS:
            raise ValueError(f"{name}.yaml: field {fname}: unknown parser {spec.get('parser')!r}")
        if "column" not in spec and "from_hints" not in spec:
            raise ValueError(f"{name}.yaml: field {fname}: needs 'column' or 'from_hints'")
    for fname, rule in (data.get("derivations") or {}).items():
        if rule not in DERIVATIONS:
            raise ValueError(f"{name}.yaml: unknown derivation {rule!r} for {fname}")
    for rule in data.get("repairs") or []:
        if rule not in REPAIRS:
            raise ValueError(f"{name}.yaml: unknown repair {rule!r}")
    return Mapping(
        name=name,
        source_table=data["source_table"],
        target=data["target"],
        fields=data["fields"],
        source_family=data.get("source_family"),
        derivations=data.get("derivations") or {},
        repairs=data.get("repairs") or [],
        checks=data.get("checks") or {},
        extra={k: v for k, v in data.items() if k not in _KNOWN_KEYS},
    )


# --------------------------------------------------------------------------------- parser registry
# Each adapter returns (value, hints) or a ParseFailure; value None = missing.

Adapter = Callable[[Any, dict[str, Any], dict[str, dict[str, str]]], Any]


def _plain(fn: Callable[..., Any]) -> Adapter:
    def run(text: Any, spec: dict[str, Any], lookups: dict[str, dict[str, str]]) -> Any:
        v = fn(text)
        return v if isinstance(v, P.ParseFailure) else (v, {})

    return run


def _money(text: Any, spec: dict[str, Any], lookups: dict[str, dict[str, str]]) -> Any:
    v = P.parse_money(text, allow_negative=bool(spec.get("allow_negative")))
    return v if isinstance(v, P.ParseFailure) else (v, {})


def _int(text: Any, spec: dict[str, Any], lookups: dict[str, dict[str, str]]) -> Any:
    v = P.parse_int(text, allow_negative=bool(spec.get("allow_negative")))
    return v if isinstance(v, P.ParseFailure) else (v, {})


def _decimal(text: Any, spec: dict[str, Any], lookups: dict[str, dict[str, str]]) -> Any:
    v = P.parse_decimal(text)
    return v if isinstance(v, P.ParseFailure) else (v, {})


def _rate(text: Any, spec: dict[str, Any], lookups: dict[str, dict[str, str]]) -> Any:
    v = P.parse_rate(text, unit=spec["unit"], max_fraction=Decimal(str(spec.get("max_fraction", 1))))
    return v if isinstance(v, P.ParseFailure) else (v, {})


def _date(text: Any, spec: dict[str, Any], lookups: dict[str, dict[str, str]]) -> Any:
    v = P.parse_date_fmt(text)
    if v is None or isinstance(v, P.ParseFailure):
        return v if v is not None else (None, {})
    return v[0], {"date_format": v[1]}


def _payment(text: Any, spec: dict[str, Any], lookups: dict[str, dict[str, str]]) -> Any:
    v = P.parse_payment(text)
    if v is None or isinstance(v, P.ParseFailure):
        return v if v is not None else (None, {})
    return v.cents, {"frequency": v.frequency_hint, "has_cents": v.has_cents}


def _term(text: Any, spec: dict[str, Any], lookups: dict[str, dict[str, str]]) -> Any:
    v = P.parse_term(text)
    if v is None or isinstance(v, P.ParseFailure):
        return v if v is not None else (None, {})
    return v.bdays, {"frequency": v.frequency_hint}


def _code(text: Any, spec: dict[str, Any], lookups: dict[str, dict[str, str]]) -> Any:
    v = P.parse_upper_code(text)
    allowed = spec.get("allowed")
    if v is not None and allowed and v not in allowed:
        return P.ParseFailure("value_not_allowed", str(text))
    return v, {}


def _lookup(text: Any, spec: dict[str, Any], lookups: dict[str, dict[str, str]]) -> Any:
    table = spec["table"]
    v = P.parse_lookup(text, lookups[table], reason=f"{table}_unmapped")
    return v if isinstance(v, P.ParseFailure) else (v, {})


PARSERS: dict[str, Adapter] = {
    "text": _plain(P.parse_text),
    "code": _code,
    "int": _int,
    "money": _money,
    "decimal": _decimal,
    "rate": _rate,
    "factor": _plain(P.parse_factor),
    "date": _date,
    "frequency": _plain(P.parse_frequency),
    "term": _term,
    "payment": _payment,
    "ein": _plain(P.parse_ein),
    "state": _plain(P.parse_state),
    "lookup": _lookup,
}


# ------------------------------------------------------------------------- derivations and repairs


def _derive_term_rtr_over_payment(values: dict[str, Any]) -> tuple[Any, str | None]:
    """term (business days) such that installment(RTR, term, frequency) == stated payment, exactly."""
    rtr, pay, freq = values.get("rtr_cents"), values.get("payment_cents"), values.get("frequency")
    if not rtr or not pay or freq not in (P.FREQ_DAILY, P.FREQ_WEEKLY):
        return None, "term_underivable_missing_inputs"
    k = P.BDAYS_PER_WEEK if freq == P.FREQ_WEEKLY else 1
    guess = P.half_up_div(rtr * k, pay)
    exact = [n for n in range(max(1, guess - 2), guess + 3) if P.installment_cents(rtr, n, freq) == pay]
    if not exact:
        return None, "term_underivable_no_exact_match"
    best = min(exact, key=lambda n: abs(n - guess))
    return best, None


DERIVATIONS: dict[str, Callable[[dict[str, Any]], tuple[Any, str | None]]] = {
    "rtr_over_payment": _derive_term_rtr_over_payment,
}


def _repair_payment_whole_dollar(values: dict[str, Any], hints: dict[str, dict[str, Any]]) -> Any:
    """Whole-dollar installment text -> the installment implied by RTR / term, if within $0.50."""
    if hints.get("payment_cents", {}).get("has_cents", True):
        return None
    rtr, term, pay, freq = (values.get(k) for k in ("rtr_cents", "term_bdays", "payment_cents", "frequency"))
    if not (rtr and term and pay is not None and freq):
        return None
    implied = P.installment_cents(rtr, term, freq)
    if implied != pay and abs(implied - pay) <= 50:
        return implied
    return None


REPAIRS: dict[str, tuple[str, Callable[[dict[str, Any], dict[str, dict[str, Any]]], Any]]] = {
    "payment_whole_dollar_from_rtr_term": ("payment_cents", _repair_payment_whole_dollar),
}


def _show(v: Any) -> str | None:
    if v is None:
        return None
    return v.isoformat() if hasattr(v, "isoformat") else str(v)


# ------------------------------------------------------------------------------------- apply a row

_MISS = object()


def apply_row(
    mapping: Mapping,
    raw: dict[str, Any],
    lookups: dict[str, dict[str, str]],
    cache: dict[tuple[str, Any], Any] | None = None,
) -> RowResult:
    """Map one raw row. ``cache`` (optional, per mapping) memoizes parser results by (field, text);
    safe because parsers are pure functions of the text and the field's spec."""
    values: dict[str, Any] = {}
    hints: dict[str, dict[str, Any]] = {}
    logs: list[LogEntry] = []
    reject: LogEntry | None = None

    def fail(fname: str, spec: dict[str, Any], reason: str, raw_text: Any) -> None:
        nonlocal reject
        values[fname] = None
        entry = LogEntry(fname, ACTION_REJECTED if spec.get("required") else ACTION_NULLED, reason, raw_text)
        if spec.get("required") and reject is None:
            reject = entry
        logs.append(entry)

    # 1) columns
    for fname, spec in mapping.fields.items():
        if "column" not in spec:
            continue
        text = raw.get(spec["column"])
        if cache is None:
            res = PARSERS[spec["parser"]](text, spec, lookups)
        else:
            ck = (fname, text)
            res = cache.get(ck, _MISS)
            if res is _MISS:
                res = cache[ck] = PARSERS[spec["parser"]](text, spec, lookups)
        if isinstance(res, P.ParseFailure):
            fail(fname, spec, res.reason, text)
            continue
        value, h = res if res is not None else (None, {})
        values[fname] = value
        hints[fname] = h
        if "format_to" in spec:
            values[spec["format_to"]] = h.get("date_format")
        if value is None and spec.get("required"):
            fail(fname, spec, "missing_required", text)

    # 2) values from hints (frequency for the 2023 tracker)
    for fname, spec in mapping.fields.items():
        if "from_hints" not in spec:
            continue
        seen = {hints.get(src, {}).get("frequency") for src in spec["from_hints"]} - {None}
        if len(seen) > 1:
            fail(fname, spec, "conflicting_hints", ",".join(sorted(seen)))
        elif seen:
            values[fname] = seen.pop()
        else:
            values[fname] = spec.get("default")

    # 3) derivations (fields the source does not have)
    for fname, rule in mapping.derivations.items():
        value, err = DERIVATIONS[rule](values)
        values[fname] = value
        if err:
            logs.append(LogEntry(fname, ACTION_NULLED, err, None))
        else:
            logs.append(LogEntry(fname, ACTION_DERIVED, f"{fname}_derived_{rule}", None, _show(value)))

    # 4) repairs (lossy values fixed by a documented rule)
    for rule in mapping.repairs:
        fname, fn = REPAIRS[rule]
        fixed = fn(values, hints)
        if fixed is not None:
            col = mapping.fields.get(fname, {}).get("column")
            logs.append(LogEntry(fname, ACTION_REPAIRED, rule, raw.get(col) if col else None, _show(fixed)))
            values[fname] = fixed

    return RowResult(values, logs, reject)
