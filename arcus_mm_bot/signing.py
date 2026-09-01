from __future__ import annotations

import json
import time
from typing import Any

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from .models import Market
from .scaling import to_quantums, to_ticks


OP_PLACE = 1
OP_CANCEL = 2
OP_MODIFY = 3

SIDE = {"BUY": 0, "SELL": 1}
TIF = {"GTT": 0, "FOK": 1, "IOC": 2, "ALO": 3}


def now_ns() -> int:
    return time.time_ns()


def now_us() -> int:
    return time.time_ns() // 1000


def canonical_json(obj: dict[str, Any]) -> str:
    return json.dumps(obj, separators=(",", ":"), sort_keys=True)


def compact_typed(fields: dict[str, Any]) -> str:
    """Scheme-1 typed payload: sorted keys, omit None, no whitespace."""
    clean = {k: v for k, v in fields.items() if v is not None}
    return json.dumps(clean, separators=(",", ":"), sort_keys=True)


class Signer:
    def __init__(self, secret_hex: str) -> None:
        raw = bytes.fromhex(secret_hex)
        if len(raw) != 32:
            raise ValueError("ARCUS_API_SECRET must decode to 32 bytes")
        self._key = Ed25519PrivateKey.from_private_bytes(raw)

    def sign_hex(self, message: str | bytes) -> str:
        data = message.encode("utf-8") if isinstance(message, str) else message
        return self._key.sign(data).hex()

    def sign_typed(self, fields: dict[str, Any]) -> tuple[str, str]:
        payload = compact_typed(fields)
        return payload, self.sign_hex(payload)

    def sign_legacy(self, timestamp_ns: int, action: str, body: dict[str, Any]) -> str:
        message = f"{timestamp_ns}{action}{canonical_json(body)}"
        return self.sign_hex(message)


def far_gtt_us(days: int = 90) -> int:
    return now_us() + days * 86_400 * 1_000_000


def place_typed(
    *,
    address: str,
    account_index: int,
    ts_ns: int,
    gtt_us: int,
    market_id: int,
    price: str,
    quantity: str,
    reduce_only: bool,
    side: str,
    tif: str,
    market: Market,
    client_id: str | None,
) -> str:
    fields: dict[str, Any] = {
        "ad": address.lower(),
        "ai": int(account_index),
        "ct": int(ts_ns),
        "g": int(gtt_us) * 1000,
        "m": int(market_id),
        "op": OP_PLACE,
        "p": to_ticks(price, market),
        "q": to_quantums(quantity, market),
        "r": 1 if reduce_only else 0,
        "s": SIDE[side],
        "t": TIF[tif],
        "v": 1,
    }
    if client_id:
        fields["c"] = client_id
    return compact_typed(fields)


def cancel_typed(
    *,
    address: str,
    account_index: int,
    ts_ns: int,
    market_id: int,
    order_id: str | None,
    client_id: str | None,
) -> str:
    if bool(order_id) == bool(client_id):
        raise ValueError("cancel requires exactly one of order_id or client_id")
    fields: dict[str, Any] = {
        "ad": address.lower(),
        "ai": int(account_index),
        "ct": int(ts_ns),
        "m": int(market_id),
        "op": OP_CANCEL,
        "v": 1,
    }
    if order_id:
        fields["id"] = order_id
    if client_id:
        fields["c"] = client_id
    return compact_typed(fields)


def modify_typed(
    *,
    address: str,
    account_index: int,
    ts_ns: int,
    gtt_us: int,
    market_id: int,
    price: str,
    quantity: str,
    reduce_only: bool,
    side: str,
    tif: str,
    market: Market,
    order_id: str | None,
    client_id: str | None,
) -> str:
    if bool(order_id) == bool(client_id):
        raise ValueError("modify requires exactly one of order_id or client_id")
    fields: dict[str, Any] = {
        "ad": address.lower(),
        "ai": int(account_index),
        "ct": int(ts_ns),
        "g": int(gtt_us) * 1000,
        "m": int(market_id),
        "op": OP_MODIFY,
        "p": to_ticks(price, market),
        "q": to_quantums(quantity, market),
        "r": 1 if reduce_only else 0,
        "s": SIDE[side],
        "t": TIF[tif],
        "v": 1,
    }
    if order_id:
        fields["id"] = order_id
    if client_id:
        fields["c"] = client_id
    return compact_typed(fields)
