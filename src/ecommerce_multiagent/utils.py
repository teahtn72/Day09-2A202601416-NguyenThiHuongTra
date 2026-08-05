from __future__ import annotations

from datetime import datetime
from decimal import Decimal, ROUND_HALF_UP
from typing import Iterable, TypeVar

T = TypeVar("T")
CENT = Decimal("0.01")


def decimal(value: str | int | float | Decimal) -> Decimal:
    return value if isinstance(value, Decimal) else Decimal(str(value))


def money(value: Decimal) -> float:
    """Round money with decimal arithmetic, never binary floating-point arithmetic."""
    return float(value.quantize(CENT, rounding=ROUND_HALF_UP))


def parse_timestamp(value: str | None) -> datetime | None:
    return datetime.strptime(value, "%Y-%m-%d %H:%M:%S") if value else None


def hours_between(later: str | None, earlier: str | None) -> float | None:
    later_dt, earlier_dt = parse_timestamp(later), parse_timestamp(earlier)
    if later_dt is None or earlier_dt is None:
        return None
    hours = decimal((later_dt - earlier_dt).total_seconds()) / decimal(3600)
    return money(hours)


def unique(values: Iterable[T], limit: int | None = None) -> list[T]:
    result: list[T] = []
    seen: set[T] = set()
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        result.append(value)
        if limit is not None and len(result) >= limit:
            break
    return result
