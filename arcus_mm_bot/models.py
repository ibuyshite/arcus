from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any


def d(value: Any) -> Decimal:
    if isinstance(value, Decimal):
        return value
    if value is None or value == "":
        return Decimal("0")
    return Decimal(str(value))


@dataclass
class Market:
    id: int
    symbol: str
    tick_size: Decimal
    step_size: Decimal
    min_order_size: Decimal
    max_order_size: Decimal
    min_order_notional: Decimal
    mark_price: Decimal
    oracle_price: Decimal
    status: str
    max_leverage: int

    @classmethod
    def from_api(cls, raw: dict[str, Any]) -> "Market":
        imf = d(raw.get("initialMarginFraction") or "0.02")
        max_lev = 50
        if imf > 0:
            max_lev = max(1, min(1000, int((Decimal("1") / imf).to_integral_value())))
        return cls(
            id=int(raw["marketId"]),
            symbol=str(raw.get("marketDisplayName") or raw.get("symbol") or raw["marketId"]),
            tick_size=d(raw["tickSize"]),
            step_size=d(raw["stepSize"]),
            min_order_size=d(raw.get("minOrderSize") or raw["stepSize"]),
            max_order_size=d(raw.get("maxOrderSize") or "0"),
            min_order_notional=d(raw.get("minOrderNotional") or "0"),
            mark_price=d(raw.get("markPrice") or "0"),
            oracle_price=d(raw.get("oraclePrice") or "0"),
            status=str(raw.get("status") or ""),
            max_leverage=max_lev,
        )


@dataclass
class Level:
    price: Decimal
    size: Decimal
