from __future__ import annotations

import asyncio
import json
import signal
from typing import Any

from arcus_mm_bot.book import OrderBook
from arcus_mm_bot.client import ArcusWS, Signer, fetch_market, fetch_open_orders, fetch_positions
from arcus_mm_bot.config import Config
from arcus_mm_bot.signing import now_us
from arcus_mm_bot.strategy import InstantCloseMarketMaker


config = Config.from_env()
config.assert_live_ready()

book = OrderBook()
strategy: InstantCloseMarketMaker
ws_client: ArcusWS
command_queue: asyncio.PriorityQueue[tuple[int, int, dict, str]]
command_seq = 0
command_sender_task: asyncio.Task | None = None
dms_task: asyncio.Task | None = None
shutdown_event = asyncio.Event()
last_ack_reason: dict[int, str] = {}


async def _command_sender() -> None:
    while not shutdown_event.is_set():
        try:
            _priority, _seq, prepared, reason = await asyncio.wait_for(
                command_queue.get(), timeout=0.25
            )
        except asyncio.TimeoutError:
            continue
        try:
            if config.dry_run:
                print(f"[dry-run] {reason}: {json.dumps(prepared.get('body', {}), separators=(',', ':'))}")
                command_queue.task_done()
                continue
            while not ws_client.ready:
                await asyncio.sleep(0.05)
                if shutdown_event.is_set():
                    return
            print(f"[send] {reason} method={prepared['method']}")
            req_id = await ws_client.submit_prepared(prepared)
            last_ack_reason[req_id] = reason
            ack = await ws_client.wait_ack(req_id, timeout=5.0)
            strategy.on_ack(prepared["method"], ack, reason)
        except Exception as exc:
            print(f"[command-sender] {exc}")
        finally:
            command_queue.task_done()


def send_prepared(prepared: dict, reason: str, priority: int) -> bool:
    global command_seq
    if config.dry_run:
        print(f"[dry-run] {reason}")
        command_seq += 1
        command_queue.put_nowait((priority, command_seq, prepared, reason))
        return True
    if not ws_client.ready:
        print(f"[send-blocked] {reason}")
        return False
    command_seq += 1
    command_queue.put_nowait((priority, command_seq, prepared, reason))
    return True


def _contents(message: dict[str, Any]) -> dict[str, Any]:
    raw = message.get("contents") or message.get("result") or {}
    return raw if isinstance(raw, dict) else {}


def _rows(payload: Any) -> list[dict[str, Any]]:
    if payload is None:
        return []
    if isinstance(payload, list):
        return [x for x in payload if isinstance(x, dict)]
    if isinstance(payload, dict):
        for key in ("orders", "positions", "fills", "data", "updates"):
            val = payload.get(key)
            if isinstance(val, list):
                return [x for x in val if isinstance(x, dict)]
            if isinstance(val, dict):
                return [x for x in val.values() if isinstance(x, dict)]
        if "orderId" in payload or "side" in payload or "size" in payload:
            return [payload]
    return []


async def on_open(client: ArcusWS) -> None:
    symbol = config.market
    addr = config.address
    await client.subscribe("bbo", symbol)
    await client.subscribe("l2Orderbook", symbol, nLevels=20)
    if addr:
        idx = config.account_index
        await client.subscribe("orders", addr, accountIndex=idx, market=symbol)
        await client.subscribe("positions", addr, accountIndex=idx, market=symbol)
        await client.subscribe(
            "userFills", addr, accountIndex=idx, market=symbol, snapshot=False
        )
        await client.subscribe("account", addr, accountIndex=idx)

    if not config.dry_run:
        _, lev = client.set_leverage(
            market_id=strategy.market.id,
            leverage=max(1, min(int(config.leverage_x), strategy.market.max_leverage)),
        )
        send_prepared(lev, f"set leverage {int(config.leverage_x)}x", 1)
        strategy.cancel_all_market("startup reconcile")
        await asyncio.sleep(0.05)
        try:
            strategy.on_positions(fetch_positions(config))
        except Exception as exc:
            print(f"[bootstrap] positions rest failed: {exc}")


async def on_disconnect() -> None:
    print("[ws] quoting paused until reconnect; resting ALO orders stay live until DMS or cancel")
    book.ready = False
    book.last_sequence_id = None


async def on_message(message: dict[str, Any]) -> None:
    mtype = message.get("type")
    channel = message.get("channel")
    contents = _contents(message)

    if mtype in {"subscribed", "channel_data"} or channel:
        if channel == "bbo":
            book.apply_bbo(contents)
            strategy.requote()
        elif channel == "l2Orderbook":
            book.apply_snapshot(contents)
            strategy.requote()
        elif channel == "l2OrderbookUpdates":
            if mtype == "subscribed" or contents.get("isSnapshot") or book.last_sequence_id is None:
                book.apply_snapshot(contents)
            else:
                book.apply_delta(contents)
            strategy.requote()
        elif channel == "orders":
            for order in _rows(contents):
                strategy.on_order(order)
            strategy.requote()
        elif channel == "positions":
            strategy.on_positions(_rows(contents) or _rows(contents.get("positions")))
        elif channel == "userFills":
            if mtype == "subscribed":
                # Snapshot is historical. Ignore it; only live channel_data after this.
                strategy.mark_fills_live()
                print("[ws] userFills live — ignoring snapshot history")
            else:
                if not strategy.accept_fills:
                    strategy.mark_fills_live()
                for fill in _rows(contents):
                    strategy.on_fill(fill)
                strategy.requote()
        elif channel == "account":
            nested = contents.get("positions")
            if nested:
                strategy.on_positions(_rows(nested))

    err = message.get("error")
    if mtype == "error" or (err not in (None, "", {}, [])):
        print(f"[ws-error] {err}")


async def dms_loop() -> None:
    """Refresh the dead-man switch. Venue does not cancel-on-disconnect."""
    if config.dry_run:
        return
    while not shutdown_event.is_set():
        try:
            if ws_client.ready:
                deadline = now_us() + config.dms_lead_seconds * 1_000_000
                _, prepared = ws_client.schedule_cancel(deadline_us=deadline)
                send_prepared(prepared, f"DMS arm +{config.dms_lead_seconds}s", 0)
        except Exception as exc:
            print(f"[dms] {exc}")
        try:
            await asyncio.wait_for(shutdown_event.wait(), timeout=config.dms_refresh_seconds)
        except asyncio.TimeoutError:
            continue


async def graceful_shutdown() -> None:
    print("[shutdown] flattening and canceling")
    shutdown_event.set()
    if not config.dry_run and ws_client.ready:
        try:
            strategy.cancel_all_market("shutdown")
            if strategy.position.size > 0:
                strategy._ensure_close()
            await asyncio.sleep(0.4)
        except Exception as exc:
            print(f"[shutdown] {exc}")


async def main() -> None:
    global strategy, ws_client, command_queue, command_sender_task, dms_task

    market = fetch_market(config)
    signer = None if config.dry_run else Signer(config.api_secret_hex)
    ws_client = ArcusWS(config, signer)
    command_queue = asyncio.PriorityQueue()
    strategy = InstantCloseMarketMaker(config, market, book, ws_client, send_prepared)

    print(
        f"loaded {market.symbol} id={market.id} env={config.env_name} "
        f"mode={'dry-run' if config.dry_run else 'LIVE'} "
        f"tick={market.tick_size} step={market.step_size} "
        f"min_size={market.min_order_size} min_notional={market.min_order_notional}"
    )
    print(
        f"[strategy] EXACT BBO offset={config.quote_offset_bps}bps "
        f"size={config.quote_size} lev={config.leverage_x}x "
        f"requote={config.requote_interval_ms}ms "
        f"close_slip={config.max_close_slippage_bps}bps "
        f"max_spread={config.max_spread_bps}bps "
        f"emergency={config.allow_emergency_market_close} "
        f"dms={config.dms_lead_seconds}s/{config.dms_refresh_seconds}s"
    )
    print(
        "Behaviour: ALO quote both sides at BBO → maker fill → "
        "priority cancel opposite + IOC reduce-only close → wait flat → re-quote."
    )
    print(
        "Latency path: WS entry, ALO skips 50ms taker bump, cancels on priority lane, "
        "cancel-by-clientId (no orderId wait). No cancel-on-disconnect — DMS is mandatory."
    )

    if not config.dry_run:
        try:
            rest_pos = fetch_positions(config)
            strategy.on_positions(rest_pos)
            extras = fetch_open_orders(config)
            print(f"[bootstrap] rest open-orders={len(extras)} positions={len(rest_pos)}")
        except Exception as exc:
            print(f"[bootstrap] rest snapshot failed: {exc}")

    command_sender_task = asyncio.create_task(_command_sender())
    dms_task = asyncio.create_task(dms_loop())

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, lambda: asyncio.create_task(graceful_shutdown()))
        except NotImplementedError:
            pass

    try:
        await ws_client.connect_forever(on_open, on_message, on_disconnect)
    finally:
        await graceful_shutdown()
        for task in (command_sender_task, dms_task):
            if task:
                task.cancel()
        await asyncio.gather(
            *[t for t in (command_sender_task, dms_task) if t],
            return_exceptions=True,
        )


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nBot stopped by user.")
