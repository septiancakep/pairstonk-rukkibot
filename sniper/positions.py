"""Manajemen posisi setelah beli: take profit bertingkat, stop loss, trailing,
dan batas waktu tahan."""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field

from .codec import swap_base_input_output
from .config import Config
from .constants import LAMPORTS_PER_SOL
from .filters import Target
from .notify import Notifier
from .trader import Trader

log = logging.getLogger("sniper.position")

# Penjualan bisa gagal karena alasan sementara (blockhash basi, jaringan padat),
# jadi dicoba lagi. Tapi kalau token memang tidak bisa dijual — honeypot,
# likuiditas sudah ditarik — mencoba selamanya hanya membakar biaya transaksi.
MAX_FAILED_SELLS = 8


@dataclass(slots=True)
class Position:
    target: Target
    tokens_held: int
    quote_spent: int
    opened_at: float = field(default_factory=time.monotonic)
    quote_recovered: int = 0
    peak_value: int = 0
    ladder_index: int = 0
    failed_sells: int = 0
    closed: bool = False

    @property
    def base_mint_short(self) -> str:
        return f"{str(self.target.base_mint)[:6]}…"


class PositionManager:
    """Satu task per posisi; masing-masing memantau harganya sendiri.

    Menaruh tiap posisi di task terpisah membuat token yang sekarat tidak
    menunggu giliran di belakang token lain saat harus dijual.
    """

    def __init__(self, cfg: Config, trader: Trader, notifier: Notifier) -> None:
        self._cfg = cfg
        self._trader = trader
        self._notifier = notifier
        self._tasks: set[asyncio.Task[None]] = set()
        self.open_count = 0

    def track(self, position: Position) -> None:
        self.open_count += 1
        task = asyncio.create_task(self._watch(position), name=f"pos-{position.base_mint_short}")
        self._tasks.add(task)
        task.add_done_callback(self._on_done)

    def _on_done(self, task: "asyncio.Task[None]") -> None:
        self._tasks.discard(task)
        self.open_count = max(0, self.open_count - 1)
        if not task.cancelled() and task.exception() is not None:
            log.error("pemantauan posisi berhenti karena error", exc_info=task.exception())

    async def close_all(self) -> None:
        for task in list(self._tasks):
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)

    async def _current_value(self, position: Position) -> int | None:
        """Nilai posisi dalam satuan quote kalau dijual seluruhnya sekarang."""
        reserves = await self._trader.reserves_for(position.target)
        if reserves is None:
            return None
        fee_rate = self._trader.trade_fee_rate(position.target.keys.amm_config)
        return swap_base_input_output(
            position.tokens_held, reserves.base, reserves.quote, fee_rate
        )

    async def _watch(self, position: Position) -> None:
        cfg = self._cfg
        interval = cfg.position_poll_ms / 1000.0
        deadline = position.opened_at + cfg.max_hold_seconds

        while not position.closed:
            await asyncio.sleep(interval)

            value = await self._current_value(position)
            if value is None:
                continue
            position.peak_value = max(position.peak_value, value)

            pnl = (value - position.quote_spent) / position.quote_spent
            reason = self._exit_reason(position, value, pnl, deadline)
            if reason is None:
                continue

            portion_bps = self._portion_for(position, reason)
            amount = position.tokens_held * portion_bps // 10_000
            if amount <= 0:
                position.closed = True
                break

            log.info(
                "%s: %s (PnL %+.1f%%), jual %.1f%% posisi",
                position.base_mint_short,
                reason,
                pnl * 100,
                portion_bps / 100,
            )
            result = await self._trader.sell(position.target, amount)
            if not result.ok:
                position.failed_sells += 1
                if position.failed_sells >= MAX_FAILED_SELLS:
                    log.error(
                        "%s: menyerah setelah %d penjualan gagal (terakhir: %s). "
                        "Token mungkin tidak bisa dijual — periksa manual.",
                        position.base_mint_short,
                        position.failed_sells,
                        result.error,
                    )
                    self._notifier.send_soon(
                        f"⚠️ <b>{position.base_mint_short} tidak bisa dijual</b>\n"
                        f"{position.failed_sells}x gagal — {result.error}\n"
                        f"Pool: <code>{position.target.keys.pool_state}</code>"
                    )
                    position.closed = True
                    break
                log.warning(
                    "penjualan gagal (%s), percobaan %d/%d",
                    result.error, position.failed_sells, MAX_FAILED_SELLS,
                )
                continue

            position.failed_sells = 0

            position.tokens_held -= amount
            # Taksiran, bukan angka on-chain: hanya dipakai untuk log penutup.
            position.quote_recovered += result.expected_out
            if reason == "take profit":
                position.ladder_index += 1
            if position.tokens_held <= 0 or reason != "take profit":
                position.closed = True

            self._notifier.send_soon(
                f"💸 <b>Jual {position.base_mint_short}</b> — {reason}\n"
                f"PnL: {pnl * 100:+.1f}%\n"
                f"Diterima: ~{result.expected_out / LAMPORTS_PER_SOL:.4f} SOL\n"
                f"Tx: <code>{result.signature}</code>"
            )

        total = position.quote_recovered
        log.info(
            "posisi %s ditutup: keluar %.4f SOL, masuk %.4f SOL (%+.1f%%)",
            position.base_mint_short,
            position.quote_spent / LAMPORTS_PER_SOL,
            total / LAMPORTS_PER_SOL,
            (total - position.quote_spent) / position.quote_spent * 100,
        )

    def _exit_reason(
        self, position: Position, value: int, pnl: float, deadline: float
    ) -> str | None:
        cfg = self._cfg
        if cfg.stop_loss_pct > 0 and pnl <= -cfg.stop_loss_pct:
            return "stop loss"
        if cfg.trailing_stop_pct > 0 and position.peak_value > position.quote_spent:
            drop = (position.peak_value - value) / position.peak_value
            if drop >= cfg.trailing_stop_pct:
                return "trailing stop"
        if position.ladder_index < len(cfg.take_profit_ladder):
            gain, _ = cfg.take_profit_ladder[position.ladder_index]
            if pnl >= gain:
                return "take profit"
        if time.monotonic() >= deadline:
            return "batas waktu tahan"
        return None

    def _portion_for(self, position: Position, reason: str) -> int:
        if reason == "take profit":
            _, portion_bps = self._cfg.take_profit_ladder[position.ladder_index]
            return portion_bps
        return 10_000
