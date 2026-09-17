"""Normalizes extracted field values toward their schema-declared type.

The extraction model reports values the way the document writes them -
"83,880.00", "9%", "₹1,250.50", "Yes" - but `view_<schema>` projects each
field to its declared SQL type, so those strings have to be castable or the
column is useless (and, before the safe-cast functions in migration 0003,
a single one of them broke the whole view for every row).

Contract: normalize only when the intent is unambiguous, otherwise leave
the raw value untouched. `extracted_records.data` stays the source of
truth, so preserving an unparseable value (and letting the typed column
read NULL for that one cell) is always better than discarding it.
"""

from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation
from typing import Any

# Anything that isn't part of a number: currency symbols/codes, unit
# suffixes, percent signs, spaces, non-breaking spaces.
_NON_NUMERIC = re.compile(r"[^0-9.,\-+]")
# "1,234" / "1,234,567" - comma used as a thousands separator.
_THOUSANDS = re.compile(r"^\d{1,3}(,\d{3})+$")

_TRUE_WORDS = {"true", "t", "yes", "y", "1", "paid", "on"}
_FALSE_WORDS = {"false", "f", "no", "n", "0", "unpaid", "off"}


def _to_decimal(raw: str) -> Decimal | None:
    text = _NON_NUMERIC.sub("", raw.strip())
    if not text:
        return None

    negative = raw.strip().startswith("(") and raw.strip().endswith(")")  # (1,200.00) accounting negative

    if "," in text and "." in text:
        # Both present: the comma groups thousands, the dot is the decimal.
        text = text.replace(",", "")
    elif "," in text:
        # Ambiguous on its own: "1,234" is thousands, "1,5" is a decimal
        # comma. Only the grouped pattern is unambiguous.
        text = text.replace(",", "") if _THOUSANDS.match(text) else text.replace(",", ".")

    text = text.lstrip("+")
    try:
        value = Decimal(text)
    except (InvalidOperation, ValueError):
        return None
    return -value if negative and value > 0 else value


def _coerce_number(raw: str) -> str | None:
    value = _to_decimal(raw)
    return format(value, "f") if value is not None else None


def _coerce_integer(raw: str) -> str | None:
    value = _to_decimal(raw)
    if value is None:
        return None
    # "1250.00" is a legitimate integer; "1250.5" is not - don't silently round.
    return str(int(value)) if value == value.to_integral_value() else None


def _coerce_boolean(raw: str) -> str | None:
    text = raw.strip().lower()
    if text in _TRUE_WORDS:
        return "true"
    if text in _FALSE_WORDS:
        return "false"
    return None


# Dates are deliberately left alone: Postgres already parses the formats
# these documents use ("24 Sep 2026", "2026-09-24"), and guessing at
# ambiguous ones (03/04/2026) would invent data. safe_date() nulls out
# whatever it genuinely can't read.
_COERCERS = {
    "number": _coerce_number,
    "integer": _coerce_integer,
    "boolean": _coerce_boolean,
}


def coerce_value(value: Any, field_type: str) -> Any:
    """Returns the normalized value, or `value` unchanged when it can't be
    normalized confidently."""
    coercer = _COERCERS.get(field_type)
    if coercer is None or value is None:
        return value
    raw = value if isinstance(value, str) else str(value)
    if not raw.strip():
        return None
    return coercer(raw) or value


def coerce_extracted_values(fields: list[dict[str, Any]], values: dict[str, Any]) -> dict[str, Any]:
    """Normalizes `values` against the schema version's `fields`. Fields the
    schema doesn't declare (an ad hoc shape, or a field dropped by a later
    version) pass through as-is."""
    types_by_name = {f["name"]: f.get("type", "string") for f in fields or []}
    return {name: coerce_value(value, types_by_name.get(name, "string")) for name, value in (values or {}).items()}
