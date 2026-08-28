"""Orkestrator sniper: detektor -> filter -> beli -> pantau posisi."""

from __future__ import annotations

import asyncio
import logging
import signal
import time
from collections import deque

from .blockhash import BlockhashCache
from .config import Config, load_config
from .constants import LAMPORTS_PER_SOL, TOKEN_ACCOUNT_RENT_LAMPORTS, WSOL_MINT
from .detect import Detector, PoolEvent
from .filters import Rejected, Target, screen_local, screen_mint_authority
from .notify import Notifier
from .positions import Position, PositionManager
from .rpc import RpcPool
from .sender import Sender
from .trader import Trader

log = logging.getLogger("sniper.main")


def setup_logging(level: str) -> None:
    logging.basicConfig(
        format="%(asctime)s %(levelname)-7s %(name)-16s %(message)s",
        datefmt="%H:%M:%S",
        level=getattr(logging, level, logging.INFO),
    )
    logging.getLogger("aiohttp").setLevel(logging.WARNING)


class RateLimiter:
    """Jendela geser sederhana untuk membatasi jumlah pembelian per menit."""

    def __init__(self, max_per_minute: int) -> None:
        self._max = max_per_minute
        self._events: deque[float] = deque()

    def allow(self) -> bool:
        now = time.monotonic()
        while self._events and now - self._events[0] > 60.0:
            self._events.popleft()
        if len(self._events) >= self._max:
            return False
        self._events.append(now)
        return True


class Sniper:
    def __init__(self, cfg: Config, rpc: RpcPool) -> None:
        self.cfg = cfg
        self.rpc = rpc
        self.blockhash = BlockhashCache(rpc)
        self.sender = Sender(
            rpc,
            jito_enable=cfg.jito_enable,
            jito_url=cfg.jito_url,
            jito_tip_lamports=cfg.jito_tip_lamports,
        )
        self.trader = Trader(cfg, rpc, self.sender, self.blockhash)
        self.notifier = Notifier(rpc, cfg.telegram_bot_token, cfg.telegram_chat_id)
        self.positions = PositionManager(cfg, self.trader, self.notifier)
        self.limiter = RateLimiter(cfg.max_buys_per_minute)
        self.spent_lamports = 0
        self.pending_buys = 0
        self._buy_tasks: set[asyncio.Task[None]] = set()

    # --- persiapan ---------------------------------------------------------

    async def preflight(self) -> None:
        cfg = self.cfg
        sol = await self.rpc.get_balance(cfg.pubkey)
        log.info("wallet %s — saldo %.4f SOL", cfg.pubkey, sol / LAMPORTS_PER_SOL)

        for quote_mint in cfg.quote_mints:
            ata = self.trader.quote_ata(quote_mint)
            balance = await self.trader.token_balance(ata)
            label = "WSOL" if quote_mint == WSOL_MINT else str(quote_mint)[:6]
            log.info("ATA quote %s: %s (saldo %d)", label, ata, balance)
            if quote_mint == WSOL_MINT and balance < cfg.buy_lamports:
                log.warning(
                    "saldo WSOL (%.4f) lebih kecil dari BUY_SOL_AMOUNT (%.4f). "
                    "Jalankan `python sniper_bot.py prepare` untuk membungkus SOL.",
                    balance / LAMPORTS_PER_SOL,
                    cfg.buy_sol,
                )

        # Tiap pembelian membuat satu token account baru; rentnya keluar dari SOL
        # asli, bukan dari WSOL, jadi saldo SOL tetap harus cukup.
        needed = TOKEN_ACCOUNT_RENT_LAMPORTS * cfg.max_open_positions + 10_000_000
        if sol < needed:
            log.warning(
                "saldo SOL tipis: butuh ~%.4f SOL untuk rent token account + biaya",
                needed / LAMPORTS_PER_SOL,
            )

        await self.sender.refresh_jito_tip_accounts()
        await self.blockhash.start()
        log.info("cache blockhash siap (umur %.2fs)", self.blockhash.age_seconds)

        if cfg.dry_run:
            log.warning("DRY_RUN aktif — bot hanya mensimulasi, tidak ada dana bergerak.")

    # --- jalur panas -------------------------------------------------------

    def _capacity_reason(self) -> str | None:
        cfg = self.cfg
        # Pembelian berjalan asinkron, jadi slotnya dipesan saat beli dimulai —
        # bukan saat posisi terdaftar — supaya beberapa pool yang muncul
        # berbarengan tidak menembus batas bersama-sama.
        in_flight = self.positions.open_count + self.pending_buys
        if in_flight >= cfg.max_open_positions:
            return f"sudah {in_flight} posisi terbuka/dalam proses"
        if self.spent_lamports + cfg.buy_lamports > int(cfg.max_total_spend_sol * LAMPORTS_PER_SOL):
            return "batas MAX_TOTAL_SPEND_SOL tercapai"
        if not self.limiter.allow():
            return "batas MAX_BUYS_PER_MINUTE tercapai"
        return None

    async def on_pool(self, event: PoolEvent) -> None:
        keys = event.keys
        try:
            target = screen_local(keys, self.cfg)
        except Rejected as exc:
            log.debug("lewati pool %s: %s", keys.pool_state, exc)
            return

        reason = self._capacity_reason()
        if reason is not None:
            log.info("lewati %s: %s", target.base_mint, reason)
            return

        detect_ms = (time.monotonic() - event.detected_at) * 1000
        log.info(
            "POOL BARU %s (quote %.3f SOL) — terdeteksi via %s, +%.0fms sejak notifikasi",
            target.base_mint,
            target.quote_reserve / LAMPORTS_PER_SOL if target.quote_is_sol else 0,
            event.source,
            detect_ms,
        )

        if self.cfg.check_mint_authority:
            try:
                await screen_mint_authority(target, self.rpc)
            except Rejected as exc:
                log.info("lewati %s: %s", target.base_mint, exc)
                return

        self.spent_lamports += self.cfg.buy_lamports
        self.pending_buys += 1
        task = asyncio.create_task(self._execute_buy(target, event))
        self._buy_tasks.add(task)
        task.add_done_callback(self._buy_tasks.discard)

    async def _execute_buy(self, target: Target, event: PoolEvent) -> None:
        try:
            await self._buy_and_track(target, event)
        finally:
            self.pending_buys = max(0, self.pending_buys - 1)

    async def _buy_and_track(self, target: Target, event: PoolEvent) -> None:
        result = await self.trader.buy(target)
        total_ms = (time.monotonic() - event.detected_at) * 1000

        if not result.ok:
            # Dana tidak jadi keluar, jadi kuota belanja dikembalikan.
            self.spent_lamports = max(0, self.spent_lamports - self.cfg.buy_lamports)
            log.warning("pembelian %s gagal: %s", target.base_mint, result.error)
            return

        log.info(
            "BELI SUKSES %s dalam %.0fms — tx %s",
            target.base_mint,
            total_ms,
            result.signature,
        )
        self.notifier.send_soon(
            f"🎯 <b>Sniped {str(target.base_mint)[:8]}…</b>\n"
            f"Masuk: {result.amount_in / LAMPORTS_PER_SOL:.4f} SOL\n"
            f"Latensi: {total_ms:.0f} ms\n"
            f"Pool: <code>{target.keys.pool_state}</code>\n"
            f"Tx: <code>{result.signature}</code>"
        )

        # Saldo nyata bisa berbeda dari taksiran (slippage, fee transfer), jadi
        # posisi selalu dibuka dari saldo ATA yang sebenarnya.
        tokens = await self.trader.token_balance(self.trader.base_ata(target))
        if tokens <= 0:
            log.warning("tidak ada token diterima untuk %s", target.base_mint)
            return
        self.positions.track(
            Position(target=target, tokens_held=tokens, quote_spent=result.amount_in)
        )

    async def run(self) -> None:
        await self.preflight()
        detector = Detector(self.cfg.rpc_ws_url, self.rpc, self.cfg.detector)
        log.info("sniper aktif — memantau program CPMM lewat mode '%s'", self.cfg.detector)
        async for event in detector.stream():
            try:
                await self.on_pool(event)
            except Exception:  # noqa: BLE001 - satu pool buruk tidak boleh menjatuhkan bot
                log.exception("kesalahan saat memproses pool %s", event.keys.pool_state)

    async def shutdown(self) -> None:
        await self.positions.close_all()
        await self.blockhash.stop()


async def run_sniper(cfg: Config) -> None:
    async with RpcPool(cfg.rpc_http_urls) as rpc:
        sniper = Sniper(cfg, rpc)
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, stop.set)
            except NotImplementedError:  # Windows
                pass

        runner = asyncio.create_task(sniper.run())
        stopper = asyncio.create_task(stop.wait())
        done, _ = await asyncio.wait(
            {runner, stopper}, return_when=asyncio.FIRST_COMPLETED
        )
        runner.cancel()
        stopper.cancel()
        await sniper.shutdown()
        for task in done:
            if task is runner and not task.cancelled() and task.exception():
                raise task.exception()  # type: ignore[misc]
        log.info("sniper berhenti.")


def main() -> None:
    cfg = load_config()
    setup_logging(cfg.log_level)
    asyncio.run(run_sniper(cfg))
