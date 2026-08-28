"""Konfigurasi sniper. Semua lewat environment variable / file .env."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

from solders.keypair import Keypair
from solders.pubkey import Pubkey

from .constants import USDC_MINT, WSOL_MINT


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def _env_bool(name: str, default: bool) -> bool:
    raw = _env(name)
    if not raw:
        return default
    return raw.lower() in {"1", "true", "yes", "y", "on"}


def _env_int(name: str, default: int) -> int:
    raw = _env(name)
    return int(raw) if raw else default


def _env_float(name: str, default: float) -> float:
    raw = _env(name)
    return float(raw) if raw else default


def _env_list(name: str, default: str = "") -> list[str]:
    raw = _env(name, default)
    return [p.strip() for p in raw.split(",") if p.strip()]


def load_dotenv(path: str | Path = ".env") -> None:
    """Muat file .env sederhana ke os.environ tanpa menimpa yang sudah ada."""
    p = Path(path)
    if not p.exists():
        return
    for line in p.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def parse_keypair(raw: str) -> Keypair:
    """Terima private key format base58 (Phantom) atau array JSON (solana-keygen)."""
    raw = raw.strip()
    if not raw:
        raise ValueError("PRIVATE_KEY kosong")
    if raw.startswith("["):
        return Keypair.from_bytes(bytes(json.loads(raw)))
    if raw.startswith("/") or raw.endswith(".json"):
        return Keypair.from_bytes(bytes(json.loads(Path(raw).read_text())))
    return Keypair.from_base58_string(raw)


def parse_ladder(raw: str) -> list[tuple[float, int]]:
    """'100:50,300:50' -> [(1.0, 5000), (3.0, 5000)].

    Kunci = kenaikan harga dalam persen, nilai = porsi sisa posisi yang dijual
    dalam persen. Diurutkan menaik supaya tangga TP dieksekusi berurutan.
    """
    steps: list[tuple[float, int]] = []
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        gain_str, _, portion_str = part.partition(":")
        gain = float(gain_str) / 100.0
        portion_bps = int(round(float(portion_str or "100") * 100))
        steps.append((gain, max(1, min(10_000, portion_bps))))
    steps.sort(key=lambda s: s[0])
    return steps


@dataclass(slots=True)
class Config:
    # --- wallet & jaringan ---
    keypair: Keypair
    rpc_http_urls: list[str]
    rpc_ws_url: str
    detector: str = "logs"  # "tx" (transactionSubscribe) atau "logs"

    # --- ukuran & toleransi order ---
    buy_sol: float = 0.05
    slippage_bps: int = 1_500
    sell_slippage_bps: int = 2_500

    # --- biaya prioritas ---
    compute_unit_limit: int = 120_000
    compute_unit_price: int = 500_000  # micro-lamports per CU
    sell_compute_unit_price: int = 200_000

    # --- Jito ---
    jito_enable: bool = False
    jito_url: str = "https://mainnet.block-engine.jito.wtf/api/v1"
    jito_tip_lamports: int = 1_000_000

    # --- filter keamanan ---
    quote_mints: list[Pubkey] = field(default_factory=lambda: [WSOL_MINT])
    min_quote_liquidity_sol: float = 0.5
    max_quote_liquidity_sol: float = 500.0
    allow_token_2022: bool = False
    check_mint_authority: bool = True
    max_open_delay_sec: int = 60
    blacklist_creators: set[str] = field(default_factory=set)
    blacklist_mints: set[str] = field(default_factory=set)

    # --- manajemen posisi ---
    take_profit_ladder: list[tuple[float, int]] = field(default_factory=list)
    stop_loss_pct: float = 0.5
    trailing_stop_pct: float = 0.0
    max_hold_seconds: int = 600
    position_poll_ms: int = 900

    # --- pembatas risiko ---
    max_open_positions: int = 3
    max_buys_per_minute: int = 6
    max_total_spend_sol: float = 1.0
    dry_run: bool = True

    # --- lain-lain ---
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""
    state_path: Path = Path("data/sniper_state.json")
    log_level: str = "INFO"

    @property
    def pubkey(self) -> Pubkey:
        return self.keypair.pubkey()

    @property
    def buy_lamports(self) -> int:
        return int(self.buy_sol * 1_000_000_000)


_QUOTE_ALIASES = {"sol": WSOL_MINT, "wsol": WSOL_MINT, "usdc": USDC_MINT}


def _parse_quote_mints(values: list[str]) -> list[Pubkey]:
    out: list[Pubkey] = []
    for v in values:
        alias = _QUOTE_ALIASES.get(v.lower())
        out.append(alias if alias else Pubkey.from_string(v))
    return out or [WSOL_MINT]


def load_config() -> Config:
    load_dotenv(_env("SNIPER_ENV_FILE", ".env"))

    rpc_http = _env_list("RPC_HTTP_URLS") or _env_list(
        "RPC_HTTP_URL", "https://api.mainnet-beta.solana.com"
    )
    ws_url = _env("RPC_WS_URL") or rpc_http[0].replace("https://", "wss://").replace(
        "http://", "ws://"
    )

    cfg = Config(
        keypair=parse_keypair(_env("PRIVATE_KEY")),
        rpc_http_urls=rpc_http,
        rpc_ws_url=ws_url,
        detector=_env("DETECTOR", "logs").lower(),
        buy_sol=_env_float("BUY_SOL_AMOUNT", 0.05),
        slippage_bps=_env_int("SLIPPAGE_BPS", 1_500),
        sell_slippage_bps=_env_int("SELL_SLIPPAGE_BPS", 2_500),
        compute_unit_limit=_env_int("COMPUTE_UNIT_LIMIT", 120_000),
        compute_unit_price=_env_int("COMPUTE_UNIT_PRICE", 500_000),
        sell_compute_unit_price=_env_int("SELL_COMPUTE_UNIT_PRICE", 200_000),
        jito_enable=_env_bool("JITO_ENABLE", False),
        jito_url=_env("JITO_URL", "https://mainnet.block-engine.jito.wtf/api/v1"),
        jito_tip_lamports=_env_int("JITO_TIP_LAMPORTS", 1_000_000),
        quote_mints=_parse_quote_mints(_env_list("QUOTE_MINTS", "sol")),
        min_quote_liquidity_sol=_env_float("MIN_QUOTE_LIQUIDITY_SOL", 0.5),
        max_quote_liquidity_sol=_env_float("MAX_QUOTE_LIQUIDITY_SOL", 500.0),
        allow_token_2022=_env_bool("ALLOW_TOKEN_2022", False),
        check_mint_authority=_env_bool("CHECK_MINT_AUTHORITY", True),
        max_open_delay_sec=_env_int("MAX_OPEN_DELAY_SEC", 60),
        blacklist_creators=set(_env_list("BLACKLIST_CREATORS")),
        blacklist_mints=set(_env_list("BLACKLIST_MINTS")),
        take_profit_ladder=parse_ladder(_env("TAKE_PROFIT_LADDER", "100:50,300:100")),
        stop_loss_pct=_env_float("STOP_LOSS_PCT", 50.0) / 100.0,
        trailing_stop_pct=_env_float("TRAILING_STOP_PCT", 0.0) / 100.0,
        max_hold_seconds=_env_int("MAX_HOLD_SECONDS", 600),
        position_poll_ms=_env_int("POSITION_POLL_MS", 900),
        max_open_positions=_env_int("MAX_OPEN_POSITIONS", 3),
        max_buys_per_minute=_env_int("MAX_BUYS_PER_MINUTE", 6),
        max_total_spend_sol=_env_float("MAX_TOTAL_SPEND_SOL", 1.0),
        dry_run=_env_bool("DRY_RUN", True),
        telegram_bot_token=_env("TELEGRAM_BOT_TOKEN"),
        telegram_chat_id=_env("TELEGRAM_CHAT_ID") or _env("TELEGRAM_CHANNEL_ID"),
        state_path=Path(_env("SNIPER_STATE_PATH", "data/sniper_state.json")),
        log_level=_env("LOG_LEVEL", "INFO").upper(),
    )
    return cfg
