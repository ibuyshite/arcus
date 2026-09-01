from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from .models import Level, d


@dataclass
class SweepResult:
    ok: bool
    available_size: Decimal
    price: Decimal | None = None
    vwap: Decimal | None = None


class OrderBook:
    """Local L2 book. Snapshot replace + incremental deltas with gap detection."""

    def __init__(self) -> None:
        self._bids: dict[Decimal, Decimal] = {}
        self._asks: dict[Decimal, Decimal] = {}
        self.last_sequence_id: int | None = None
        self.ready = False
        self._bbo_bid: Decimal | None = None
        self._bbo_ask: Decimal | None = None

    def apply_snapshot(self, contents: dict) -> None:
        self._bids = {}
        self._asks = {}
        self._bbo_bid = None
        self._bbo_ask = None
        self._merge(contents.get("bids") or [], self._bids)
        self._merge(contents.get("asks") or [], self._asks)
        seq = contents.get("lastSequenceId")
        self.last_sequence_id = int(seq) if seq is not None else None
        self.ready = True

    def apply_delta(self, contents: dict) -> bool:
        """Return False if a sequence gap was detected (caller should resubscribe)."""
        seq = contents.get("lastSequenceId")
        if seq is not None and self.last_sequence_id is not None:
            incoming = int(seq)
            if incoming <= self.last_sequence_id:
                return True
            if incoming > self.last_sequence_id + 1:
                self.ready = False
                return False
            self.last_sequence_id = incoming
        elif seq is not None:
            self.last_sequence_id = int(seq)

        self._merge(contents.get("bids") or [], self._bids)
        self._merge(contents.get("asks") or [], self._asks)
        self.ready = True
        return True

    def apply_bbo(self, contents: dict) -> None:
        """Replace top-of-book only. Do not leave a stale previous best in the map."""
        bid = contents.get("bestBid") or {}
        ask = contents.get("bestAsk") or {}
        if bid.get("price") is not None:
            price = d(bid["price"])
            size = d(bid.get("size") or "0")
            if self._bbo_bid is not None and self._bbo_bid != price:
                self._bids.pop(self._bbo_bid, None)
            if size <= 0:
                self._bids.pop(price, None)
                self._bbo_bid = None
            else:
                self._bids[price] = size
                self._bbo_bid = price
        if ask.get("price") is not None:
            price = d(ask["price"])
            size = d(ask.get("size") or "0")
            if self._bbo_ask is not None and self._bbo_ask != price:
                self._asks.pop(self._bbo_ask, None)
            if size <= 0:
                self._asks.pop(price, None)
                self._bbo_ask = None
            else:
                self._asks[price] = size
                self._bbo_ask = price
        self.ready = True

    def best_bid(self) -> Level | None:
        bids = self.bids()
        return bids[0] if bids else None

    def best_ask(self) -> Level | None:
        asks = self.asks()
        return asks[0] if asks else None

    def bids(self) -> list[Level]:
        return [
            Level(price=p, size=s)
            for p, s in sorted(self._bids.items(), key=lambda kv: kv[0], reverse=True)
            if s > 0
        ]

    def asks(self) -> list[Level]:
        return [
            Level(price=p, size=s)
            for p, s in sorted(self._asks.items(), key=lambda kv: kv[0])
            if s > 0
        ]

    def _merge(self, raw_levels: list, target: dict[Decimal, Decimal]) -> None:
        for raw in raw_levels:
            if not raw:
                continue
            if isinstance(raw, dict):
                price = d(raw.get("price") or raw.get("p"))
                size = d(raw.get("size") or raw.get("s") or "0")
            else:
                price = d(raw[0])
                size = d(raw[1])
            if size <= 0:
                target.pop(price, None)
            else:
                target[price] = size


def sweep_for_ioc(
    levels: list[Level],
    size: Decimal,
    side: str,
    reference_price: Decimal,
    max_slippage_bps: float,
) -> SweepResult:
    remaining = size
    filled = Decimal("0")
    notional = Decimal("0")
    last_price: Decimal | None = None

    for level in levels:
        take = min(remaining, level.size)
        if take <= 0:
            continue
        filled += take
        notional += take * level.price
        remaining -= take
        last_price = level.price
        if remaining <= 0:
            break

    if filled < size or last_price is None:
        return SweepResult(ok=False, available_size=filled)

    vwap = notional / filled
    bps = Decimal(str(max_slippage_bps)) / Decimal("10000")
    if side == "buy":
        ok = vwap <= reference_price * (Decimal("1") + bps)
    else:
        ok = vwap >= reference_price * (Decimal("1") - bps)
    return SweepResult(ok=ok, available_size=filled, price=last_price, vwap=vwap)
