from __future__ import annotations

import time
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Callable

from .book import OrderBook, sweep_for_ioc
from .client import ArcusWS
from .config import Config
from .models import Market, d
from .scaling import clamp_slippage_price, dec_str, fmt_price, fmt_size, snap_price, snap_size


SendPrepared = Callable[[dict, str, int], bool]


TERMINAL_STATUS = {
    "FILLED",
    "CANCELED",
    "CANCELLED",
    "REJECTED",
    "EXPIRED",
    "FAILED",
}

LIVE_STATUS = {"ACK", "OPEN", "PENDING", "PARTIAL", "PARTIALLY_FILLED", "NEW"}


@dataclass
class ManagedQuote:
    side: str  # bid | ask
    client_id: str | None = None
    order_id: str | None = None
    price: Decimal | None = None
    size: Decimal = Decimal("0")
    pending: bool = False


@dataclass
class PositionState:
    side: str | None = None  # long | short
    size: Decimal = Decimal("0")
    entry_price: Decimal | None = None


class InstantCloseMarketMaker:
    """
    Maker-open at top of book + instant taker flatten.

    1. One ALO bid and one ALO ask at (or offset from) BBO.
    2. Maker fill → cancel opposite immediately (by clientId, no wait for orderId).
    3. Positions are inventory truth → IOC reduce-only close of full size.
    4. Flat → requote both sides.
    """

    def __init__(
        self,
        config: Config,
        market: Market,
        book: OrderBook,
        ws: ArcusWS,
        send_prepared: SendPrepared,
    ) -> None:
        self.config = config
        self.market = market
        self.book = book
        self.ws = ws
        self.send_prepared = send_prepared

        self.quotes = {
            "bid": ManagedQuote(side="bid"),
            "ask": ManagedQuote(side="ask"),
        }
        self.position = PositionState()
        self.inventory_mode = False
        self.last_requote_at = 0.0
        self.close_pending = False
        self.close_client_id: str | None = None
        self.close_retry_at = 0.0
        self.close_attempts = 0
        self.cid_seq = 0
        self.seen_order_ids: set[str] = set()
        self.live_client_ids: set[str] = set()
        self.accept_fills = False

    def mark_fills_live(self) -> None:
        """Call after the userFills snapshot is discarded so only new fills count."""
        self.accept_fills = True

    def _is_our_cid(self, cid: str | None) -> bool:
        if not cid:
            return False
        if cid in self.live_client_ids:
            return True
        return len(cid) >= 2 and cid[0] in {"b", "a", "c"} and cid[1:].isdigit()

    def _cid(self, prefix: str) -> str:
        self.cid_seq += 1
        return f"{prefix}{self.cid_seq}"

    def _quote_size(self) -> Decimal:
        size = snap_size(d(self.config.quote_size), self.market)
        cap = snap_size(d(self.config.max_open_position), self.market)
        if size <= 0:
            raise ValueError("QUOTE_SIZE snapped to zero; increase size or check stepSize")
        if size > cap:
            raise ValueError("QUOTE_SIZE cannot be greater than MAX_OPEN_POSITION")
        if size < self.market.min_order_size:
            raise ValueError(
                f"QUOTE_SIZE {size} < minOrderSize {self.market.min_order_size}"
            )
        return size

    # ------------------------------------------------------------------
    # Position
    # ------------------------------------------------------------------
    def on_positions(self, rows: list[dict[str, Any]]) -> None:
        our = None
        for pos in rows or []:
            mid = pos.get("marketId")
            name = str(pos.get("marketDisplayName") or pos.get("market") or "")
            if mid is not None and int(mid) != self.market.id and name != self.market.symbol:
                continue
            if name and name != self.market.symbol and mid is None:
                continue
            size = abs(d(pos.get("size") or pos.get("s") or "0"))
            side = str(pos.get("side") or "").upper()
            if size > 0 and side in {"LONG", "SHORT"}:
                our = pos
                break

        if our is None:
            was_open = self.position.size > 0 or self.inventory_mode
            if was_open and self.position.size > 0:
                print(
                    f"[position] FLAT (was {self.position.side} "
                    f"{fmt_size(self.position.size, self.market)})"
                )
            self.position = PositionState()
            self.inventory_mode = False
            self.close_pending = False
            self.close_client_id = None
            self.close_attempts = 0
            if was_open:
                self.last_requote_at = 0
            return

        side = str(our.get("side") or "").lower()
        size = abs(d(our.get("size") or "0"))
        entry = d(our.get("averageEntryPrice") or our.get("entryPrice") or "0") or None
        if side != self.position.side or size != self.position.size:
            print(
                f"[position] {side} size={fmt_size(size, self.market)} "
                f"entry={fmt_price(entry, self.market) if entry else '-'}"
            )
        self.position = PositionState(side=side, size=size, entry_price=entry)
        if size > 0:
            self.inventory_mode = True
            self._cancel_all_quotes("inventory detected")
            self._ensure_close()

    # ------------------------------------------------------------------
    # Orders / fills
    # ------------------------------------------------------------------
    def on_order(self, order: dict[str, Any]) -> None:
        mid = order.get("marketId")
        name = str(order.get("marketDisplayName") or "")
        if mid is not None and int(mid) != self.market.id and name != self.market.symbol:
            return

        oid = str(order.get("orderId") or "") or None
        cid = str(order.get("clientId") or "") or None
        status = str(order.get("status") or order.get("state") or "").upper()
        side_raw = str(order.get("side") or "").upper()
        price = d(order.get("price") or "0") or None
        remaining = d(order.get("remainingSize") or "0")
        reason = str(order.get("rejectionReason") or order.get("error") or "")

        if cid and cid == self.close_client_id:
            if status in TERMINAL_STATUS:
                print(f"[close] {status} cid={cid} reason={reason or '-'}")
                self.close_pending = False
                self.close_client_id = None
                if status in {"REJECTED", "CANCELED", "CANCELLED", "EXPIRED", "FAILED"}:
                    self.close_attempts += 1
                    self._retry_close_soon()
            return

        matched: ManagedQuote | None = None
        for quote in self.quotes.values():
            if (cid and quote.client_id == cid) or (oid and quote.order_id == oid):
                matched = quote
                break

        if matched is None:
            if status in LIVE_STATUS and side_raw in {"BUY", "SELL"} and oid:
                side = "bid" if side_raw == "BUY" else "ask"
                candidate = self.quotes[side]
                ours = self._is_our_cid(cid)
                can_adopt = (
                    ours
                    or (
                        candidate.order_id is None
                        and not self.inventory_mode
                        and (not candidate.pending or candidate.client_id in {None, cid})
                    )
                )
                if can_adopt and (candidate.order_id in {None, oid}):
                    candidate.order_id = oid
                    candidate.client_id = cid or candidate.client_id
                    candidate.price = price
                    candidate.size = remaining or d(order.get("originalSize") or "0")
                    candidate.pending = False
                    if cid:
                        self.live_client_ids.add(cid)
                    print(f"[reconcile] adopt {side} oid={oid} cid={cid}")
                elif oid and oid not in self.seen_order_ids and not ours:
                    print(f"[reconcile] cancel extra {side_raw} oid={oid} cid={cid}")
                    self._cancel_ids(oid, cid, f"extra {side_raw}")
            if oid:
                self.seen_order_ids.add(oid)
            return

        if oid:
            matched.order_id = oid
            self.seen_order_ids.add(oid)
        if price:
            matched.price = price
        if remaining:
            matched.size = remaining

        if status in TERMINAL_STATUS:
            print(
                f"[order] {matched.side} {status} oid={oid or '-'} cid={cid or '-'} "
                f"reason={reason or '-'}"
            )
            matched.order_id = None
            matched.client_id = None
            matched.price = None
            matched.size = Decimal("0")
            matched.pending = False
            if "CROSS" in reason.upper() or status == "REJECTED":
                self.last_requote_at = 0
            return

        matched.pending = False

    def on_fill(self, fill: dict[str, Any]) -> None:
        if not self.accept_fills:
            return

        mid = fill.get("marketId")
        name = str(fill.get("marketDisplayName") or "")
        if mid is not None and int(mid) != self.market.id and name != self.market.symbol:
            return

        size = abs(d(fill.get("size") or fill.get("filledSize") or "0"))
        if size <= 0:
            return

        oid = str(fill.get("orderId") or "") or None
        cid = str(fill.get("clientId") or "") or None
        ours = False
        for quote in self.quotes.values():
            if (cid and quote.client_id == cid) or (oid and quote.order_id == oid):
                ours = True
                break
        if not ours:
            ours = self._is_our_cid(cid)
        if not ours:
            return

        role = str(fill.get("role") or fill.get("liquidity") or fill.get("l") or "").upper()
        side = str(fill.get("side") or "").upper()
        price = d(fill.get("price") or "0")
        reduce_only = bool(fill.get("reduceOnly"))
        print(
            f"[fill] role={role or '?'} side={side} size={fmt_size(size, self.market)} "
            f"@ {fmt_price(price, self.market) if price else '-'} cid={cid or '-'}"
        )

        if role == "MAKER" and not reduce_only and side in {"BUY", "SELL"}:
            qside = "bid" if side == "BUY" else "ask"
            q = self.quotes[qside]
            q.order_id = None
            q.client_id = None
            q.price = None
            q.size = Decimal("0")
            q.pending = False
            self.inventory_mode = True
            opposite = "ask" if qside == "bid" else "bid"
            self._cancel_quote(opposite, "maker fill on opposite")
            self._ensure_close()

    def on_ack(self, method: str, message: dict[str, Any], reason: str) -> None:
        status = message.get("status")
        result = message.get("result") if isinstance(message.get("result"), dict) else {}
        error = message.get("error") if isinstance(message.get("error"), dict) else {}
        err = (
            result.get("rejectionReason")
            or result.get("error")
            or error.get("message")
            or message.get("error")
        )
        code = status
        print(f"[ack] {method} status={code} reason={reason} err={err or '-'}")

        if method == "cancelAllOrders":
            for q in self.quotes.values():
                q.order_id = None
                q.client_id = None
                q.price = None
                q.size = Decimal("0")
                q.pending = False
            self.last_requote_at = 0
            return

        if method in {"placeOrder", "modifyOrder"} and (
            (isinstance(code, int) and code >= 400)
            or str(result.get("status") or "").upper() == "REJECTED"
        ):
            for q in self.quotes.values():
                if q.pending:
                    q.pending = False
            self.last_requote_at = 0
        if method == "placeOrder" and "CROSS" in str(err).upper():
            self.last_requote_at = 0
            for q in self.quotes.values():
                if q.pending:
                    q.pending = False
                    q.client_id = None
                    q.order_id = None

        oid = result.get("orderId")
        cid = result.get("clientId")
        if oid or cid:
            for q in self.quotes.values():
                if cid and q.client_id == cid:
                    q.order_id = oid or q.order_id
                    q.pending = False

        if method in {"cancelOrder", "placeOrder"} and "INSTANT TAKER CLOSE" in reason:
            if isinstance(code, int) and code >= 400:
                self.close_pending = False
                self.close_attempts += 1
                self._retry_close_soon()

    # ------------------------------------------------------------------
    # Quoting
    # ------------------------------------------------------------------
    def requote(self, force: bool = False) -> None:
        if self.inventory_mode and self.position.size <= 0 and not self.close_pending:
            self.inventory_mode = False

        if self.inventory_mode or self.position.size > 0 or self.close_pending:
            if self.position.size > 0 or self.close_pending:
                self._ensure_close()
            return

        now = time.monotonic() * 1000
        if not force and now - self.last_requote_at < self.config.requote_interval_ms:
            return

        best_bid = self.book.best_bid()
        best_ask = self.book.best_ask()
        if best_bid is None or best_ask is None:
            return

        mid = (best_bid.price + best_ask.price) / 2
        if mid > 0:
            spread_bps = (best_ask.price - best_bid.price) / mid * Decimal("10000")
            if spread_bps > Decimal(str(self.config.max_spread_bps)):
                return

        quote_size = self._quote_size()
        tick = self.market.tick_size
        min_ticks = max(1, self.config.min_requote_ticks)

        if self.config.quote_offset_bps == 0:
            bid = best_bid.price
            ask = best_ask.price
        else:
            off = Decimal(str(self.config.quote_offset_bps)) / Decimal("10000")
            bid = snap_price(best_bid.price * (Decimal("1") - off), self.market, "bid")
            ask = snap_price(best_ask.price * (Decimal("1") + off), self.market, "ask")
            bid = min(bid, best_ask.price - tick)
            ask = max(ask, best_bid.price + tick)

        if bid >= ask:
            return
        if bid >= best_ask.price or ask <= best_bid.price:
            return

        changed = False
        changed |= self._upsert_quote("bid", bid, quote_size, min_ticks)
        changed |= self._upsert_quote("ask", ask, quote_size, min_ticks)
        if changed:
            self.last_requote_at = now

    def _upsert_quote(self, side: str, price: Decimal, size: Decimal, min_ticks: int) -> bool:
        quote = self.quotes[side]
        if quote.pending:
            return False
        if (
            quote.order_id
            and quote.price is not None
            and quote.size == size
            and abs(quote.price - price) < self.market.tick_size * min_ticks
        ):
            return False

        order_side = "BUY" if side == "bid" else "SELL"
        price_s = dec_str(price)
        size_s = dec_str(size)

        if self.config.dry_run:
            action = "change" if quote.order_id else "place"
            print(
                f"[dry-run] {action} {side} @ {fmt_price(price, self.market)} "
                f"size={fmt_size(size, self.market)}"
            )
            quote.price = price
            quote.size = size
            quote.order_id = quote.order_id or f"dry-{side}"
            quote.client_id = quote.client_id or f"dry-{side}"
            quote.pending = False
            return True

        if quote.order_id or quote.client_id:
            ts, prepared = self.ws.modify_order(
                market=self.market,
                side=order_side,
                price=price_s,
                quantity=size_s,
                tif="ALO",
                reduce_only=False,
                order_id=quote.order_id,
                client_id=None if quote.order_id else quote.client_id,
            )
            reason = (
                f"change {side} @ {fmt_price(price, self.market)} "
                f"size={fmt_size(size, self.market)}"
            )
        else:
            cid = self._cid("b" if side == "bid" else "a")
            ts, prepared = self.ws.place_order(
                market=self.market,
                side=order_side,
                price=price_s,
                quantity=size_s,
                tif="ALO",
                order_type="LIMIT",
                reduce_only=False,
                client_id=cid,
            )
            quote.client_id = cid
            self.live_client_ids.add(cid)
            reason = (
                f"place {side} @ {fmt_price(price, self.market)} "
                f"size={fmt_size(size, self.market)} cid={cid}"
            )
            _ = ts

        sent = self.send_prepared(prepared, reason, 1)
        if not sent:
            return False
        quote.price = price
        quote.size = size
        quote.pending = True
        return True

    # ------------------------------------------------------------------
    # Cancels
    # ------------------------------------------------------------------
    def _cancel_quote(self, side: str, why: str) -> None:
        quote = self.quotes[side]
        if quote.order_id is None and quote.client_id is None:
            quote.pending = False
            quote.price = None
            quote.size = Decimal("0")
            return
        if quote.pending:
            return
        self._cancel_ids(quote.order_id, quote.client_id, f"{side} ({why})")
        quote.pending = True

    def _cancel_all_quotes(self, why: str) -> None:
        self._cancel_quote("bid", why)
        self._cancel_quote("ask", why)

    def _cancel_ids(self, order_id: str | None, client_id: str | None, label: str) -> None:
        if self.config.dry_run:
            print(f"[dry-run] CANCEL {label} oid={order_id} cid={client_id}")
            return
        if not order_id and not client_id:
            return
        _, prepared = self.ws.cancel_order(
            market=self.market,
            order_id=order_id,
            client_id=None if order_id else client_id,
        )
        self.send_prepared(prepared, f"CANCEL {label}", 0)

    def cancel_all_market(self, why: str) -> None:
        if self.config.dry_run:
            print(f"[dry-run] CANCEL ALL {why}")
            return
        _, prepared = self.ws.cancel_all(market_id=self.market.id)
        self.send_prepared(prepared, f"CANCEL ALL ({why})", 0)

    # ------------------------------------------------------------------
    # Instant taker close
    # ------------------------------------------------------------------
    def _ensure_close(self) -> None:
        if self.position.size <= 0:
            return
        if self.close_pending:
            return
        if time.monotonic() < self.close_retry_at:
            return
        self._send_close()

    def _retry_close_soon(self) -> None:
        self.close_retry_at = time.monotonic() + 0.12

    def _send_close(self) -> None:
        size = snap_size(self.position.size, self.market)
        if size <= 0:
            return
        closing_long = self.position.side == "long"
        order_side = "SELL" if closing_long else "BUY"
        reference = self.position.entry_price
        if closing_long and self.book.best_bid():
            reference = reference or self.book.best_bid().price
            sweep = sweep_for_ioc(
                self.book.bids(),
                size,
                "sell",
                reference or self.book.best_bid().price,
                self.config.max_close_slippage_bps,
            )
        elif not closing_long and self.book.best_ask():
            reference = reference or self.book.best_ask().price
            sweep = sweep_for_ioc(
                self.book.asks(),
                size,
                "buy",
                reference or self.book.best_ask().price,
                self.config.max_close_slippage_bps,
            )
        else:
            print("[close] no book; waiting")
            self._retry_close_soon()
            return

        use_emergency = False
        if not sweep.ok:
            if self.config.allow_emergency_market_close or self.close_attempts >= 3:
                use_emergency = True
                print(
                    f"[close] thin book avail={fmt_size(sweep.available_size, self.market)}; "
                    f"emergency IOC attempt={self.close_attempts}"
                )
            else:
                print(
                    f"[close-blocked] avail={fmt_size(sweep.available_size, self.market)}; retry"
                )
                self.close_attempts += 1
                self._retry_close_soon()
                return

        if use_emergency:
            ref = (
                self.book.best_bid().price
                if closing_long and self.book.best_bid()
                else self.book.best_ask().price
                if self.book.best_ask()
                else reference
            )
            limit = clamp_slippage_price(
                ref or Decimal("0"),
                "sell" if closing_long else "buy",
                max(self.config.max_close_slippage_bps, 15),
                self.market,
            )
            order_type = "MARKET"
            tif = "IOC"
            price_s = dec_str(limit)
        else:
            order_type = "LIMIT"
            tif = "IOC"
            price_s = dec_str(sweep.price or reference or Decimal("0"))

        if self.config.dry_run:
            print(
                f"[dry-run] INSTANT TAKER CLOSE {self.position.side} "
                f"{fmt_size(size, self.market)} @ {price_s}"
                f"{' EMERGENCY' if use_emergency else ''}"
            )
            self.position = PositionState()
            self.inventory_mode = False
            return

        cid = self._cid("c")
        _, prepared = self.ws.place_order(
            market=self.market,
            side=order_side,
            price=price_s,
            quantity=dec_str(size),
            tif=tif,
            order_type=order_type,
            reduce_only=True,
            client_id=cid,
        )
        sent = self.send_prepared(
            prepared,
            f"INSTANT TAKER CLOSE {self.position.side} "
            f"@ {price_s} size={fmt_size(size, self.market)}"
            f"{' EMERGENCY' if use_emergency else ''}",
            0,
        )
        if sent:
            self.close_pending = True
            self.close_client_id = cid
            self.inventory_mode = True
        else:
            self._retry_close_soon()
