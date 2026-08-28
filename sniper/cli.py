"""Antarmuka baris perintah sniper."""

from __future__ import annotations

import argparse
import asyncio
import sys

from .blockhash import BlockhashCache
from .config import Config, load_config
from .constants import LAMPORTS_PER_SOL, TOKEN_PROGRAM_ID, WSOL_MINT
from .ixs import close_account, derive_ata, wrap_sol
from .main import run_sniper, setup_logging
from .rpc import RpcPool
from .sender import Sender, build_transaction
from .trader import Trader


async def cmd_balance(cfg: Config) -> int:
    async with RpcPool(cfg.rpc_http_urls) as rpc:
        sender = Sender(rpc)
        blockhash = BlockhashCache(rpc)
        trader = Trader(cfg, rpc, sender, blockhash)
        sol = await rpc.get_balance(cfg.pubkey)
        wsol_ata = derive_ata(cfg.pubkey, WSOL_MINT, TOKEN_PROGRAM_ID)
        wsol = await trader.token_balance(wsol_ata)
        print(f"Wallet   : {cfg.pubkey}")
        print(f"SOL      : {sol / LAMPORTS_PER_SOL:.6f}")
        print(f"WSOL ATA : {wsol_ata}")
        print(f"WSOL     : {wsol / LAMPORTS_PER_SOL:.6f}")
        print(f"Modal per snipe: {cfg.buy_sol} SOL  ->  {int(wsol / cfg.buy_lamports)} snipe tersisa")
    return 0


async def cmd_prepare(cfg: Config, amount_sol: float) -> int:
    """Buat WSOL ATA dan bungkus SOL sekali di muka.

    Menyiapkan WSOL lebih dulu memangkas tiga instruksi (create + transfer +
    sync_native) dari setiap transaksi beli — transaksi jadi lebih kecil,
    lebih murah, dan lebih cepat masuk blok.
    """
    lamports = int(amount_sol * LAMPORTS_PER_SOL)
    async with RpcPool(cfg.rpc_http_urls) as rpc:
        blockhash = BlockhashCache(rpc)
        await blockhash.start()
        sender = Sender(rpc)
        sol = await rpc.get_balance(cfg.pubkey)
        if sol < lamports + 10_000_000:
            print(
                f"Saldo SOL ({sol / LAMPORTS_PER_SOL:.4f}) tidak cukup untuk "
                f"membungkus {amount_sol} SOL plus biaya."
            )
            return 1
        tx = build_transaction(cfg.keypair, wrap_sol(cfg.pubkey, lamports), blockhash.value)
        signature, ok, error = await sender.rebroadcast_until_confirmed(tx, timeout_sec=45)
        await blockhash.stop()
        print(f"{'OK' if ok else 'GAGAL'}: {signature}" + (f" ({error})" if error else ""))
        return 0 if ok else 1


async def cmd_unwrap(cfg: Config) -> int:
    """Tutup WSOL ATA dan kembalikan seluruh saldonya jadi SOL."""
    async with RpcPool(cfg.rpc_http_urls) as rpc:
        blockhash = BlockhashCache(rpc)
        await blockhash.start()
        sender = Sender(rpc)
        ata = derive_ata(cfg.pubkey, WSOL_MINT, TOKEN_PROGRAM_ID)
        ix = close_account(ata, cfg.pubkey, cfg.pubkey, TOKEN_PROGRAM_ID)
        tx = build_transaction(cfg.keypair, [ix], blockhash.value)
        signature, ok, error = await sender.rebroadcast_until_confirmed(tx, timeout_sec=45)
        await blockhash.stop()
        print(f"{'OK' if ok else 'GAGAL'}: {signature}" + (f" ({error})" if error else ""))
        return 0 if ok else 1


async def cmd_check(cfg: Config) -> int:
    """Uji konektivitas dan tampilkan konfigurasi efektif tanpa mengirim apa pun."""
    from .constants import CPMM_AUTHORITY, CPMM_PROGRAM_ID

    print(f"Program CPMM     : {CPMM_PROGRAM_ID}")
    print(f"Authority PDA    : {CPMM_AUTHORITY}")
    print(f"Wallet           : {cfg.pubkey}")
    print(f"Detector         : {cfg.detector}")
    print(f"RPC HTTP         : {', '.join(cfg.rpc_http_urls)}")
    print(f"RPC WS           : {cfg.rpc_ws_url}")
    print(f"Quote mint       : {', '.join(str(m) for m in cfg.quote_mints)}")
    print(f"Modal per snipe  : {cfg.buy_sol} SOL, slippage {cfg.slippage_bps / 100:.2f}%")
    print(f"Priority fee     : {cfg.compute_unit_price} µlamport/CU x {cfg.compute_unit_limit} CU")
    print(f"Jito             : {'aktif' if cfg.jito_enable else 'nonaktif'}"
          + (f", tip {cfg.jito_tip_lamports / LAMPORTS_PER_SOL:.5f} SOL" if cfg.jito_enable else ""))
    print(f"TP ladder        : {cfg.take_profit_ladder}")
    print(f"Stop loss        : {cfg.stop_loss_pct * 100:.0f}%  trailing {cfg.trailing_stop_pct * 100:.0f}%")
    print(f"DRY_RUN          : {cfg.dry_run}")

    async with RpcPool(cfg.rpc_http_urls) as rpc:
        for url in cfg.rpc_http_urls:
            try:
                blockhash, height = await rpc.get_latest_blockhash()
                print(f"  [ok]   {url} -> blockhash {blockhash[:12]}… (height {height})")
            except Exception as exc:  # noqa: BLE001 - laporan diagnostik
                print(f"  [GAGAL] {url} -> {exc}")
        try:
            async with rpc.session.ws_connect(cfg.rpc_ws_url, timeout=10) as ws:
                await ws.send_json(
                    {"jsonrpc": "2.0", "id": 1, "method": "getVersion", "params": []}
                )
            print(f"  [ok]   WebSocket {cfg.rpc_ws_url}")
        except Exception as exc:  # noqa: BLE001
            print(f"  [GAGAL] WebSocket {cfg.rpc_ws_url} -> {exc}")

    if cfg.detector == "logs":
        print(
            "\nCatatan: mode 'logs' harus mengambil transaksi lewat getTransaction "
            "yang baru menjawab pada commitment confirmed — tambahan ratusan ms. "
            "Pakai DETECTOR=tx dengan RPC yang mendukung transactionSubscribe "
            "(Helius/Triton/QuickNode) untuk jalur cepat."
        )
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="sniper_bot.py", description="Sniper Raydium CPMM"
    )
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("run", help="jalankan sniper (default)")
    sub.add_parser("check", help="uji konfigurasi & koneksi RPC")
    sub.add_parser("balance", help="tampilkan saldo SOL/WSOL")
    prepare = sub.add_parser("prepare", help="bungkus SOL jadi WSOL untuk modal snipe")
    prepare.add_argument("amount", type=float, help="jumlah SOL yang dibungkus")
    sub.add_parser("unwrap", help="tutup WSOL ATA, kembalikan jadi SOL")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    cfg = load_config()
    setup_logging(cfg.log_level)

    command = args.command or "run"
    if command == "run":
        asyncio.run(run_sniper(cfg))
        return 0
    if command == "check":
        return asyncio.run(cmd_check(cfg))
    if command == "balance":
        return asyncio.run(cmd_balance(cfg))
    if command == "prepare":
        return asyncio.run(cmd_prepare(cfg, args.amount))
    if command == "unwrap":
        return asyncio.run(cmd_unwrap(cfg))
    print(f"perintah tidak dikenal: {command}", file=sys.stderr)
    return 2
