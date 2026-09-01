from __future__ import annotations

from decimal import Decimal, ROUND_CEILING, ROUND_DOWN, ROUND_FLOOR, ROUND_UP

from .models import Market, d


def snap_price(price: Decimal, market: Market, side: str) -> Decimal:
    tick = market.tick_size
    if tick <= 0:
        return price
    units = price / tick
    if side == "bid":
        snapped = units.to_integral_value(rounding=ROUND_DOWN) * tick
    else:
        snapped = units.to_integral_value(rounding=ROUND_UP) * tick
    return snapped.quantize(tick)


def snap_size(size: Decimal, market: Market) -> Decimal:
    step = market.step_size
    if step <= 0:
        return size
    units = (size / step).to_integral_value(rounding=ROUND_DOWN)
    return (units * step).quantize(step)


def to_ticks(price: Decimal | str, market: Market) -> int:
    n = d(price) / market.tick_size
    if n != n.to_integral_value():
        raise ValueError(f"price {price} is not a multiple of tick {market.tick_size}")
    return int(n)


def to_quantums(size: Decimal | str, market: Market) -> int:
    n = d(size) / market.step_size
    if n != n.to_integral_value():
        raise ValueError(f"size {size} is not a multiple of step {market.step_size}")
    return int(n)


def fmt_price(price: Decimal | None, market: Market) -> str:
    if price is None:
        return "-"
    return format(price.quantize(market.tick_size), "f")


def fmt_size(size: Decimal | None, market: Market) -> str:
    if size is None:
        return "-"
    return format(size.quantize(market.step_size), "f")


def dec_str(value: Decimal) -> str:
    return format(value, "f")


def clamp_slippage_price(reference: Decimal, side: str, bps: float, market: Market) -> Decimal:
    frac = Decimal(str(bps)) / Decimal("10000")
    if side == "sell":
        raw = reference * (Decimal("1") - frac)
        return snap_price(raw, market, "bid")
    raw = reference * (Decimal("1") + frac)
    return snap_price(raw, market, "ask")
