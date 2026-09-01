from __future__ import annotations

import asyncio
import json
from typing import Any, Awaitable, Callable
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

import websockets

from .config import Config
from .models import Market
from .signing import (
    Signer,
    cancel_typed,
    far_gtt_us,
    modify_typed,
    now_ns,
    place_typed,
)


MessageHandler = Callable[[dict[str, Any]], Awaitable[None]]


def http_json(
    method: str,
    url: str,
    body: dict | None = None,
    headers: dict[str, str] | None = None,
    timeout: int = 10,
) -> Any:
    data = None if body is None else json.dumps(body, separators=(",", ":")).encode()
    req = Request(
        url,
        data=data,
        method=method,
        headers={"Content-Type": "application/json", "User-Agent": "arcus-mm-bot/0.1", **(headers or {})},
    )
    try:
        with urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            return json.loads(raw.decode()) if raw else {}
    except HTTPError as exc:
        err_body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"HTTP {exc.code} {url}: {err_body}") from exc
    except URLError as exc:
        raise RuntimeError(f"request failed {url}: {exc}") from exc


def fetch_market(config: Config) -> Market:
    payload = http_json("GET", f"{config.rest_url}/v1/markets?market={config.market}")
    markets = payload.get("markets") or []
    if not markets:
        raise ValueError(f"market {config.market} not found")
    return Market.from_api(markets[0])


def fetch_open_orders(config: Config) -> list[dict]:
    if not config.address:
        return []
    payload = http_json(
        "GET",
        f"{config.rest_url}/v1/openOrders?address={config.address}&market={config.market}",
    )
    return payload.get("orders") or []


def fetch_positions(config: Config) -> list[dict]:
    if not config.address:
        return []
    payload = http_json("GET", f"{config.rest_url}/v1/positions?address={config.address}")
    positions = payload.get("positions")
    if isinstance(positions, dict):
        return list(positions.values())
    return positions or []


class ArcusWS:
    """Single multiplexed socket: market data + signed order entry."""

    def __init__(self, config: Config, signer: Signer | None) -> None:
        self.config = config
        self.signer = signer
        self.ws: Any | None = None
        self.ready = False
        self._req_id = 0
        self._pending: dict[int, asyncio.Future] = {}
        self._on_message: MessageHandler | None = None
        self._send_lock = asyncio.Lock()

    def _next_id(self) -> int:
        self._req_id += 1
        return self._req_id

    async def connect_forever(
        self,
        on_open: Callable[["ArcusWS"], Awaitable[None]],
        on_message: MessageHandler,
        on_disconnect: Callable[[], Awaitable[None]] | None = None,
    ) -> None:
        self._on_message = on_message
        delay = 1.0
        while True:
            try:
                async with websockets.connect(
                    self.config.ws_url,
                    ping_interval=15,
                    ping_timeout=10,
                    max_queue=1024,
                ) as ws:
                    self.ws = ws
                    self.ready = True
                    delay = 1.0
                    print("[ws] connected")
                    await on_open(self)
                    async for raw in ws:
                        await self._dispatch(json.loads(raw))
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                print(f"[ws] disconnected: {exc}")
            finally:
                self.ready = False
                self.ws = None
                for fut in list(self._pending.values()):
                    if not fut.done():
                        fut.set_exception(ConnectionError("websocket disconnected"))
                self._pending.clear()
                if on_disconnect is not None:
                    await on_disconnect()
            await asyncio.sleep(delay)
            delay = min(delay * 2, 15.0)

    async def _dispatch(self, message: dict[str, Any]) -> None:
        req_id = message.get("id")
        if req_id is not None and not isinstance(req_id, str):
            fut = self._pending.get(int(req_id))
            if fut is not None and not fut.done():
                fut.set_result(message)
        if self._on_message is not None:
            await self._on_message(message)

    async def send_raw(self, payload: dict[str, Any]) -> None:
        if self.ws is None:
            raise ConnectionError("websocket not connected")
        async with self._send_lock:
            await self.ws.send(json.dumps(payload, separators=(",", ":")))

    async def subscribe(self, channel: str, sub_id: str, **extra: Any) -> None:
        frame: dict[str, Any] = {"type": "subscribe", "channel": channel, "id": sub_id}
        frame.update({k: v for k, v in extra.items() if v is not None})
        await self.send_raw(frame)
        print(f"[ws] subscribe {channel} id={sub_id} {extra or ''}")

    async def _post(self, method: str, body: dict[str, Any], signature: str, ts_ns: int) -> int:
        if self.signer is None:
            raise RuntimeError("signer required for live posts")
        req_id = self._next_id()
        envelope = {
            "type": "post",
            "id": req_id,
            "request": {
                "type": method,
                "payload": body,
                "apiKey": self.config.api_key,
                "timestamp": str(ts_ns),
                "signature": signature,
            },
        }
        loop = asyncio.get_running_loop()
        fut: asyncio.Future = loop.create_future()
        self._pending[req_id] = fut
        await self.send_raw(envelope)
        return req_id

    async def wait_ack(self, req_id: int, timeout: float = 5.0) -> dict[str, Any]:
        fut = self._pending.get(req_id)
        if fut is None:
            return {}
        try:
            return await asyncio.wait_for(fut, timeout=timeout)
        except asyncio.TimeoutError:
            return {"id": req_id, "status": 0, "error": {"message": "ack-timeout"}}
        finally:
            self._pending.pop(req_id, None)

    def place_order(
        self,
        *,
        market: Market,
        side: str,
        price: str,
        quantity: str,
        tif: str,
        order_type: str,
        reduce_only: bool,
        client_id: str,
    ) -> tuple[int, dict]:
        ts = now_ns()
        gtt = far_gtt_us()
        typed = place_typed(
            address=self.config.address,
            account_index=self.config.account_index,
            ts_ns=ts,
            gtt_us=gtt,
            market_id=market.id,
            price=price,
            quantity=quantity,
            reduce_only=reduce_only,
            side=side,
            tif=tif,
            market=market,
            client_id=client_id,
        )
        assert self.signer is not None
        sig = self.signer.sign_hex(typed)
        body = {
            "address": self.config.address,
            "accountIndex": self.config.account_index,
            "marketId": market.id,
            "orderSide": side,
            "orderType": order_type,
            "timeInForce": tif,
            "quantity": quantity,
            "price": price,
            "goodTilTime": str(gtt),
            "timestamp": ts,
            "clientId": client_id,
            "reduceOnly": reduce_only,
        }
        return ts, {"method": "placeOrder", "body": body, "signature": sig, "typed": typed, "ts": ts}

    def modify_order(
        self,
        *,
        market: Market,
        side: str,
        price: str,
        quantity: str,
        tif: str,
        reduce_only: bool,
        order_id: str | None,
        client_id: str | None,
    ) -> tuple[int, dict]:
        ts = now_ns()
        gtt = far_gtt_us()
        typed = modify_typed(
            address=self.config.address,
            account_index=self.config.account_index,
            ts_ns=ts,
            gtt_us=gtt,
            market_id=market.id,
            price=price,
            quantity=quantity,
            reduce_only=reduce_only,
            side=side,
            tif=tif,
            market=market,
            order_id=order_id,
            client_id=client_id,
        )
        assert self.signer is not None
        sig = self.signer.sign_hex(typed)
        body: dict[str, Any] = {
            "address": self.config.address,
            "accountIndex": self.config.account_index,
            "marketId": market.id,
            "side": side,
            "timeInForce": tif,
            "quantity": quantity,
            "price": price,
            "goodTilTime": str(gtt),
            "reduceOnly": reduce_only,
            "timestamp": ts,
        }
        if order_id:
            body["orderId"] = order_id
        if client_id and not order_id:
            body["clientId"] = client_id
        return ts, {"method": "modifyOrder", "body": body, "signature": sig, "typed": typed, "ts": ts}

    def cancel_order(
        self,
        *,
        market: Market,
        order_id: str | None,
        client_id: str | None,
    ) -> tuple[int, dict]:
        ts = now_ns()
        typed = cancel_typed(
            address=self.config.address,
            account_index=self.config.account_index,
            ts_ns=ts,
            market_id=market.id,
            order_id=order_id,
            client_id=client_id,
        )
        assert self.signer is not None
        sig = self.signer.sign_hex(typed)
        body: dict[str, Any] = {
            "address": self.config.address,
            "accountIndex": self.config.account_index,
            "marketId": market.id,
            "kind": "orderId",
            "timestamp": ts,
        }
        if order_id:
            body["orderId"] = order_id
        if client_id and not order_id:
            body["clientId"] = client_id
        return ts, {"method": "cancelOrder", "body": body, "signature": sig, "typed": typed, "ts": ts}

    def cancel_all(self, *, market_id: int | None = None) -> tuple[int, dict]:
        ts = now_ns()
        body: dict[str, Any] = {
            "address": self.config.address,
            "accountIndex": self.config.account_index,
        }
        if market_id is not None:
            body["marketId"] = market_id
        assert self.signer is not None
        sig = self.signer.sign_legacy(ts, "cancelAllOrders", body)
        return ts, {"method": "cancelAllOrders", "body": body, "signature": sig, "ts": ts}

    def set_leverage(self, *, market_id: int, leverage: int) -> tuple[int, dict]:
        ts = now_ns()
        body = {
            "address": self.config.address,
            "accountIndex": self.config.account_index,
            "marketId": market_id,
            "leverage": int(leverage),
            "isolated": False,
        }
        assert self.signer is not None
        sig = self.signer.sign_legacy(ts, "setLeverage", body)
        return ts, {"method": "setLeverage", "body": body, "signature": sig, "ts": ts}

    def schedule_cancel(self, *, deadline_us: int | None) -> tuple[int, dict]:
        ts = now_ns()
        body: dict[str, Any] = {
            "address": self.config.address,
            "accountIndex": self.config.account_index,
        }
        if deadline_us is not None:
            body["time"] = int(deadline_us)
        assert self.signer is not None
        sig = self.signer.sign_legacy(ts, "scheduleCancel", body)
        return ts, {"method": "scheduleCancel", "body": body, "signature": sig, "ts": ts}

    async def submit_prepared(self, prepared: dict) -> int:
        return await self._post(
            prepared["method"],
            prepared["body"],
            prepared["signature"],
            int(prepared["ts"]),
        )
