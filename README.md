# Arcus Instant-Close Market Maker

Mainnet-only trading bot for [Arcus](https://arcus.xyz) perpetuals.

It quotes **ALO (post-only) at the best bid and best ask**. When a maker fill opens inventory, it immediately cancels the opposite quote and sends an **IOC reduce-only close**. After the account is flat it quotes both sides again.

This repo talks only to Arcus **mainnet**:

- REST `https://api.arcus.xyz`
- WebSocket `wss://api.arcus.xyz/v1/ws`

Testnet URLs are rejected on startup.

## Layout

```
.
├── main.py                 # start here
├── .env.example
├── requirements.txt
└── arcus_mm_bot/           # library (do not run files in here)
    ├── book.py
    ├── client.py
    ├── config.py
    ├── models.py
    ├── scaling.py
    ├── signing.py
    └── strategy.py
```

```bash
python main.py
```

## What it does

1. Connects to one multiplexed WebSocket (market data + order entry).
2. Subscribes to `bbo` and `l2Orderbook` for `ARCUS_MARKET`.
3. Places one ALO bid and one ALO ask at top of book (`QUOTE_OFFSET_BPS=0`).
4. On a maker fill: cancel the other side (by `clientId`, no wait for `orderId`), then IOC flatten the **full position**.
5. Positions are the source of truth. Fills are only a fast hint.
6. If the book is too thin for the close, it retries; after a few failures (or if `ALLOW_EMERGENCY_MARKET_CLOSE=true`) it sends a market-style IOC.
7. Arms Arcus `scheduleCancel` (dead-man switch) while running. **Arcus does not cancel-on-disconnect.** If the process dies, resting quotes stay live until DMS fires.

ALO skips Arcus’s 50 ms taker speed bump. The flatten leg is a taker order, so that bump still applies to closes.

## Requirements

- Python 3.11+ (3.10 may work)
- Funded Arcus mainnet account (USDG collateral)
- Ed25519 API key registered to your Ethereum address
- Linux VPS is the intended host. Arcus matching is in Asia — an Asian region gives lower RTT.

## Create an API key

1. Open the Arcus mainnet app and deposit collateral.
2. Generate an Ed25519 keypair locally (server stores only the public half):

```bash
python3 - <<'PY'
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
priv = Ed25519PrivateKey.generate()
print("ARCUS_API_SECRET=" + priv.private_bytes_raw().hex())
print("ARCUS_API_KEY=" + priv.public_key().public_bytes_raw().hex())
PY
```

3. Register that public key against your wallet with `POST /v1/createApiKey` (EIP-712 signature from the wallet). The app’s API-keys page does this for you.
4. Copy the wallet address, public key, and private key into `.env`. Never commit `.env`.

## Local / first run

```bash
git clone <your-repo>
cd <your-repo>
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
nano .env          # fill ARCUS_ADDRESS, ARCUS_API_KEY, ARCUS_API_SECRET
```

Leave `DRY_RUN=true` and start:

```bash
python main.py
```

You should see mainnet connect and dry-run quotes at BBO, for example:

```
loaded BTC-USD id=1 env=mainnet mode=dry-run ...
[ws] connected
[ws] subscribe bbo id=BTC-USD
[dry-run] place bid @ 77994.7 size=0.00100000
[dry-run] place ask @ 77994.9 size=0.00100000
```

If the spread is wider than `MAX_SPREAD_BPS` the bot stays silent on purpose.

When the quote prices look right, set `DRY_RUN=false` and restart. Start small (`QUOTE_SIZE` / `MAX_OPEN_POSITION`).

## Environment

| Variable | Default | Meaning |
|---|---|---|
| `ARCUS_MARKET` | `BTC-USD` | Market display name |
| `ARCUS_ACCOUNT_INDEX` | `0` | Subaccount `0–9` |
| `ARCUS_ADDRESS` | — | Wallet bound to the API key |
| `ARCUS_API_KEY` | — | 64-hex Ed25519 public key |
| `ARCUS_API_SECRET` | — | 64-hex Ed25519 private key |
| `DRY_RUN` | `true` | Log orders, do not send them |
| `QUOTE_SIZE` | `0.001` | Size of each ALO quote |
| `MAX_OPEN_POSITION` | `0.001` | Hard cap; must be ≥ `QUOTE_SIZE` |
| `LEVERAGE_X` | `5` | Cross leverage sent on startup |
| `QUOTE_OFFSET_BPS` | `0` | `0` = exact BBO |
| `REQUOTE_INTERVAL_MS` | `200` | Min time between quote edits |
| `MIN_REQUOTE_TICKS` | `1` | Ignore BBO moves smaller than this |
| `MAX_CLOSE_SLIPPAGE_BPS` | `5` | Max VWAP slippage on flatten |
| `MAX_SPREAD_BPS` | `20` | Do not quote if BBO is wider |
| `ALLOW_EMERGENCY_MARKET_CLOSE` | `true` | Market IOC if the book is too thin |
| `DMS_LEAD_SECONDS` | `45` | Dead-man deadline (5–300) |
| `DMS_REFRESH_SECONDS` | `15` | How often to refresh DMS |

BTC-USD minimums on mainnet are roughly `minOrderSize=0.0001` and `minOrderNotional=$5`.

## Run on a VPS

Use a small always-on Linux box. Arcus recommends at least 2 CPU / 4 GB RAM (4 / 8 if you later add more markets). Prefer an **Asia** region.

### 1. Server

Ubuntu 22.04/24.04 example:

```bash
sudo apt update
sudo apt install -y python3 python3-venv python3-pip git
```

Create a user so you are not running as root:

```bash
sudo adduser --disabled-password --gecos "" arcus
sudo su - arcus
```

### 2. Code and venv

```bash
git clone <your-repo> ~/arcus-mm
cd ~/arcus-mm
python3 -m venv .venv
source .venv/bin/activate
pip install -U pip
pip install -r requirements.txt
cp .env.example .env
chmod 600 .env
nano .env
```

Fill keys. First boot with `DRY_RUN=true`.

### 3. Smoke test

```bash
source ~/arcus-mm/.venv/bin/activate
cd ~/arcus-mm
python main.py
```

Ctrl+C after you see BBO dry-run quotes. Then set `DRY_RUN=false` if you are ready to send live ALO orders.

### 4. systemd (keeps it up after reboot / crash)

```bash
sudo tee /etc/systemd/system/arcus-mm.service >/dev/null <<'EOF'
[Unit]
Description=Arcus instant-close market maker
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=arcus
WorkingDirectory=/home/arcus/arcus-mm
Environment=PYTHONUNBUFFERED=1
ExecStart=/home/arcus/arcus-mm/.venv/bin/python /home/arcus/arcus-mm/main.py
Restart=always
RestartSec=5
LimitNOFILE=65535

# Hardening
NoNewPrivileges=true
PrivateTmp=true

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable --now arcus-mm
sudo systemctl status arcus-mm
sudo journalctl -u arcus-mm -f
```

Useful commands:

```bash
sudo systemctl stop arcus-mm
sudo systemctl restart arcus-mm
sudo journalctl -u arcus-mm -n 200 --no-pager
```

On stop/restart the process sends cancel-all and tries to flatten. If it is killed hard (`kill -9`, host crash), resting ALO orders stay on the book until **DMS** fires (`DMS_LEAD_SECONDS`). Keep those values tight.

### 5. Optional: tmux instead of systemd

```bash
sudo apt install -y tmux
tmux new -s arcus
cd ~/arcus-mm && source .venv/bin/activate && python main.py
# detach: Ctrl+b then d
# reattach: tmux attach -t arcus
```

systemd is better on a VPS.

## Safety

- Do not commit `.env`, keys, or PEM files. `.gitignore` already excludes them.
- `DRY_RUN=true` until logs look correct on live BBO.
- Size down. A maker fill is an open; the close is a taker and pays taker fees + the 50 ms bump.
- If you see `[send-blocked]` the socket is down — quoting pauses, but working orders are **not** auto-canceled.
- After a crash, check the Arcus UI for leftover orders/positions before restarting live.

## Disclaimer

Trading perpetuals can lose the entire deposit. This software is provided as-is, with no warranty. You are responsible for keys, sizing, and venue risk.
