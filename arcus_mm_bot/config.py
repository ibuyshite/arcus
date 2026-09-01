from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import os


MAINNET_REST_URL = "https://api.arcus.xyz"
MAINNET_WS_URL = "wss://api.arcus.xyz/v1/ws"


def load_env_file(path: str = ".env") -> None:
    candidates = [
        Path(path),
        Path.cwd() / ".env",
        Path(__file__).resolve().parent.parent / ".env",
    ]
    seen: set[Path] = set()
    for env_path in candidates:
        env_path = env_path.resolve()
        if env_path in seen or not env_path.exists() or not env_path.is_file():
            continue
        seen.add(env_path)
        for line in env_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def _number(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or raw == "":
        return default
    try:
        return float(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be a number") from exc


def _integer(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or raw == "":
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer") from exc


def _boolean(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None or raw == "":
        return default
    return raw.lower() in {"1", "true", "yes", "y"}


@dataclass
class Config:
    env_name: str
    rest_url: str
    ws_url: str
    market: str
    account_index: int
    address: str
    api_key: str
    api_secret_hex: str
    dry_run: bool
    quote_size: float
    leverage_x: float
    quote_offset_bps: float
    requote_interval_ms: int
    min_requote_ticks: int
    max_close_slippage_bps: float
    max_spread_bps: float
    max_open_position: float
    allow_emergency_market_close: bool
    dms_lead_seconds: int
    dms_refresh_seconds: int

    @classmethod
    def from_env(cls) -> "Config":
        load_env_file()
        env_name = (os.getenv("ARCUS_ENV") or "mainnet").strip().lower()
        if env_name and env_name != "mainnet":
            raise ValueError("This bot is mainnet-only. Unset ARCUS_ENV or set ARCUS_ENV=mainnet.")
        rest_url = (os.getenv("ARCUS_REST_URL") or MAINNET_REST_URL).rstrip("/")
        ws_url = os.getenv("ARCUS_WS_URL") or MAINNET_WS_URL
        if "testnet" in rest_url.lower() or "testnet" in ws_url.lower():
            raise ValueError("Testnet URLs are not allowed. Use https://api.arcus.xyz and wss://api.arcus.xyz/v1/ws.")
        address = (os.getenv("ARCUS_ADDRESS") or "").strip()
        if address and not address.startswith("0x"):
            address = "0x" + address
        return cls(
            env_name="mainnet",
            rest_url=rest_url,
            ws_url=ws_url,
            market=(os.getenv("ARCUS_MARKET") or "BTC-USD").strip(),
            account_index=_integer("ARCUS_ACCOUNT_INDEX", 0),
            address=address.lower(),
            api_key=(os.getenv("ARCUS_API_KEY") or "").strip().removeprefix("0x"),
            api_secret_hex=(os.getenv("ARCUS_API_SECRET") or "").strip().removeprefix("0x"),
            dry_run=_boolean("DRY_RUN", True),
            quote_size=_number("QUOTE_SIZE", 0.001),
            leverage_x=_number("LEVERAGE_X", 5),
            quote_offset_bps=_number("QUOTE_OFFSET_BPS", 0),
            requote_interval_ms=_integer("REQUOTE_INTERVAL_MS", 200),
            min_requote_ticks=_integer("MIN_REQUOTE_TICKS", 1),
            max_close_slippage_bps=_number("MAX_CLOSE_SLIPPAGE_BPS", 5),
            max_spread_bps=_number("MAX_SPREAD_BPS", 20),
            max_open_position=_number("MAX_OPEN_POSITION", 0.001),
            allow_emergency_market_close=_boolean("ALLOW_EMERGENCY_MARKET_CLOSE", True),
            dms_lead_seconds=_integer("DMS_LEAD_SECONDS", 45),
            dms_refresh_seconds=_integer("DMS_REFRESH_SECONDS", 15),
        )

    def assert_live_ready(self) -> None:
        if self.dry_run:
            return
        if not self.address or len(self.address) < 42:
            raise ValueError("ARCUS_ADDRESS is required when DRY_RUN=false")
        if len(self.api_key) != 64:
            raise ValueError("ARCUS_API_KEY must be the 64-hex Ed25519 public key")
        if len(self.api_secret_hex) != 64:
            raise ValueError("ARCUS_API_SECRET must be the 64-hex Ed25519 private key")
        if not 0 <= self.account_index <= 9:
            raise ValueError("ARCUS_ACCOUNT_INDEX must be 0-9")
        if self.dms_lead_seconds < 6 or self.dms_lead_seconds > 300:
            raise ValueError("DMS_LEAD_SECONDS must be between 6 and 300")
        if self.dms_refresh_seconds < 3:
            raise ValueError("DMS_REFRESH_SECONDS must be >= 3")
        if self.dms_refresh_seconds >= self.dms_lead_seconds:
            raise ValueError("DMS_REFRESH_SECONDS must be less than DMS_LEAD_SECONDS")
        if "testnet" in self.rest_url.lower() or "testnet" in self.ws_url.lower():
            raise ValueError("Refusing to start against testnet")
