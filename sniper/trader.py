"""Eksekusi swap: membangun, mengirim, dan mengonfirmasi transaksi beli/jual."""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass

from solders.compute_budget import set_compute_unit_limit, set_compute_unit_price
from solders.instruction import Instruction
from solders.pubkey import Pubkey

from .blockhash import BlockhashCache
from .codec import (
    PoolKeys,
    PoolState,
    apply_slippage,
    decode_pool_state,
    decode_token_amount,
    decode_trade_fee_rate,
    swap_base_input_output,
    swap_disabled,
)
from .config import Config
from .constants import LAMPORTS_PER_SOL, TOKEN_PROGRAM_ID
from .filters import Target
from .ixs import create_ata_idempotent, derive_ata, jito_tip, swap_base_input
from .rpc import RpcPool
from .sender import Sender, build_transaction

log = logging.getLogger("sniper.trader")

# Dipakai saat trade_fee_rate pool belum diketahui. Sengaja lebih tinggi dari
# fee mana pun yang dipakai Raydium: menaksir fee terlalu besar hanya membuat
# batas minimum keluaran lebih longgar, sedangkan menaksir terlalu kecil
# membuat transaksi ditolak on-chain.
CONSERVATIVE_TRADE_FEE_RATE = 10_000  # 1%


@dataclass(slots=True)
class SwapResult:
    signature: str | None
    ok: bool
    error: str | None = None
    amount_in: int = 0
    expected_out: int = 0
    min_out: int = 0
    latency_ms: float = 0.0


@dataclass(slots=True)
class Reserves:
    quote: int
    base: int


class Trader:
    def __init__(
        self,
        cfg: Config,
        rpc: RpcPool,
        sender: Sender,
        blockhash: BlockhashCache,
    ) -> None:
        self._cfg = cfg
        self._rpc = rpc
        self._sender = sender
        self._blockhash = blockhash
        self._fee_cache: dict[str, int] = {}
        self._background: set[asyncio.Task[None]] = set()

    def _spawn_background(self, coro) -> None:
        """Jalankan pekerjaan non-kritis tanpa menahan pemanggil.

        Referensinya disimpan karena asyncio hanya memegang weak reference ke
        task — tanpa ini, GC bisa membatalkannya di tengah jalan.
        """
        task = asyncio.create_task(coro)
        self._background.add(task)
        task.add_done_callback(self._background.discard)

    # --- fee ---------------------------------------------------------------

    def trade_fee_rate(self, amm_config: Pubkey) -> int:
        return self._fee_cache.get(str(amm_config), CONSERVATIVE_TRADE_FEE_RATE)

    async def warm_fee_cache(self, amm_config: Pubkey) -> None:
        """Baca trade_fee_rate satu amm_config dan simpan.

        Dipanggil setelah transaksi terkirim, bukan sebelumnya: konfigurasi AMM
        jumlahnya sedikit dan tidak berubah, jadi pool berikutnya sudah dapat
        angka tepat tanpa biaya latensi di jalur beli.
        """
        key = str(amm_config)
        if key in self._fee_cache:
            return
        try:
            data = await self._rpc.get_account_data(amm_config, "confirmed")
            if data:
                self._fee_cache[key] = decode_trade_fee_rate(data)
                log.info(
                    "amm_config %s: trade fee %.4f%%",
                    key[:8],
                    self._fee_cache[key] / 10_000,
                )
        except Exception as exc:  # noqa: BLE001 - fee konservatif tetap dipakai
            log.debug("gagal membaca amm_config %s: %s", key, exc)

    # --- pembacaan pool ----------------------------------------------------

    async def read_pool(self, keys: PoolKeys) -> tuple[PoolState, int, int] | None:
        """Ambil PoolState dan cadangan efektif kedua vault dalam satu RPC.

        Cadangan yang bisa ditukar = saldo vault dikurangi fee protokol dan fee
        fund yang masih mengendap di vault yang sama.
        """
        accounts = [keys.pool_state, keys.token_0_vault, keys.token_1_vault]
        try:
            data = await self._rpc.get_multiple_account_data(accounts, "processed")
        except Exception as exc:  # noqa: BLE001
            log.debug("gagal membaca pool %s: %s", keys.pool_state, exc)
            return None
        if len(data) < 3 or data[0] is None or data[1] is None or data[2] is None:
            return None

        state = decode_pool_state(keys.pool_state, data[0])
        vault_0 = decode_token_amount(data[1])
        vault_1 = decode_token_amount(data[2])
        reserve_0 = max(0, vault_0 - state.protocol_fees_0 - state.fund_fees_0)
        reserve_1 = max(0, vault_1 - state.protocol_fees_1 - state.fund_fees_1)
        return state, reserve_0, reserve_1

    async def reserves_for(self, target: Target) -> Reserves | None:
        result = await self.read_pool(target.keys)
        if result is None:
            return None
        state, reserve_0, reserve_1 = result
        if swap_disabled(state.status):
            return None
        # read_pool memakai urutan token_0/token_1; petakan ke quote/base.
        if target.quote_side == 0:
            return Reserves(quote=reserve_0, base=reserve_1)
        return Reserves(quote=reserve_1, base=reserve_0)

    # --- beli --------------------------------------------------------------

    def quote_ata(self, quote_mint: Pubkey) -> Pubkey:
        return derive_ata(self._cfg.pubkey, quote_mint, TOKEN_PROGRAM_ID)

    def base_ata(self, target: Target) -> Pubkey:
        _, _, base_program = target.keys.leg(target.base_side)
        return derive_ata(self._cfg.pubkey, target.base_mint, base_program)

    def _budget_ixs(self, unit_price: int) -> list[Instruction]:
        return [
            set_compute_unit_limit(self._cfg.compute_unit_limit),
            set_compute_unit_price(unit_price),
        ]

    async def buy(self, target: Target) -> SwapResult:
        cfg = self._cfg
        started = time.perf_counter()

        amount_in = cfg.buy_lamports
        fee_rate = self.trade_fee_rate(target.keys.amm_config)
        expected_out = swap_base_input_output(
            amount_in, target.quote_reserve, target.base_reserve, fee_rate
        )
        if expected_out <= 0:
            return SwapResult(None, False, "taksiran keluaran nol")
        min_out = apply_slippage(expected_out, cfg.slippage_bps)

        _, _, base_program = target.keys.leg(target.base_side)
        instructions = self._budget_ixs(cfg.compute_unit_price)
        instructions.append(
            create_ata_idempotent(cfg.pubkey, cfg.pubkey, target.base_mint, base_program)
        )
        instructions.append(
            swap_base_input(
                cfg.pubkey,
                target.keys,
                target.quote_side,
                self.quote_ata(target.quote_mint),
                self.base_ata(target),
                amount_in,
                min_out,
            )
        )
        if self._sender.jito_enable and self._sender.jito_tip_lamports > 0:
            instructions.append(
                jito_tip(
                    cfg.pubkey,
                    self._sender.random_tip_account(),
                    self._sender.jito_tip_lamports,
                )
            )

        await self._wait_for_open(target.open_time)

        if cfg.dry_run:
            log.info(
                "[DRY RUN] beli %s: %.4f SOL -> min %d unit (taksiran %d)",
                target.base_mint,
                amount_in / LAMPORTS_PER_SOL,
                min_out,
                expected_out,
            )
            return SwapResult(
                None, False, "DRY_RUN aktif — tidak ada transaksi dikirim",
                amount_in, expected_out, min_out,
                (time.perf_counter() - started) * 1000,
            )

        tx = build_transaction(cfg.keypair, instructions, self._blockhash.value)
        signature, ok, error = await self._sender.rebroadcast_until_confirmed(
            tx, timeout_sec=30.0, interval_sec=1.0
        )
        latency_ms = (time.perf_counter() - started) * 1000
        self._spawn_background(self.warm_fee_cache(target.keys.amm_config))
        return SwapResult(signature, ok, error, amount_in, expected_out, min_out, latency_ms)

    async def _wait_for_open(self, open_time: int) -> None:
        """Pool boleh dibuat dengan open_time di masa depan; swap sebelum itu gagal."""
        delay = open_time - time.time()
        if delay > 0:
            log.info("menunggu %.1fs sampai pool dibuka", delay)
            await asyncio.sleep(delay)

    # --- jual --------------------------------------------------------------

    async def token_balance(self, ata: Pubkey) -> int:
        data = await self._rpc.get_account_data(ata, "confirmed")
        if data is None:
            return 0
        try:
            return decode_token_amount(data)
        except ValueError:
            return 0

    async def sell(self, target: Target, amount_in: int) -> SwapResult:
        """Tukar `amount_in` token base kembali ke quote."""
        cfg = self._cfg
        started = time.perf_counter()
        if amount_in <= 0:
            return SwapResult(None, False, "tidak ada saldo untuk dijual")

        reserves = await self.reserves_for(target)
        if reserves is None:
            return SwapResult(None, False, "cadangan pool tidak terbaca / swap dimatikan")

        fee_rate = self.trade_fee_rate(target.keys.amm_config)
        expected_out = swap_base_input_output(
            amount_in, reserves.base, reserves.quote, fee_rate
        )
        min_out = apply_slippage(expected_out, cfg.sell_slippage_bps)

        instructions = self._budget_ixs(cfg.sell_compute_unit_price)
        # Sudah pasti ada (dibuat oleh `prepare`), tapi createIdempotent murah
        # dan menghindari penjualan gagal kalau akun itu pernah ditutup.
        instructions.append(
            create_ata_idempotent(
                cfg.pubkey, cfg.pubkey, target.quote_mint, TOKEN_PROGRAM_ID
            )
        )
        instructions.append(
            swap_base_input(
                cfg.pubkey,
                target.keys,
                target.base_side,
                self.base_ata(target),
                self.quote_ata(target.quote_mint),
                amount_in,
                min_out,
            )
        )
        if self._sender.jito_enable and self._sender.jito_tip_lamports > 0:
            instructions.append(
                jito_tip(
                    cfg.pubkey,
                    self._sender.random_tip_account(),
                    self._sender.jito_tip_lamports,
                )
            )

        if cfg.dry_run:
            log.info("[DRY RUN] jual %d unit -> min %d lamports", amount_in, min_out)
            return SwapResult(
                None, False, "DRY_RUN aktif — tidak ada transaksi dikirim",
                amount_in, expected_out, min_out,
                (time.perf_counter() - started) * 1000,
            )

        tx = build_transaction(cfg.keypair, instructions, self._blockhash.value)
        signature, ok, error = await self._sender.rebroadcast_until_confirmed(
            tx, timeout_sec=45.0, interval_sec=1.5
        )
        latency_ms = (time.perf_counter() - started) * 1000
        return SwapResult(signature, ok, error, amount_in, expected_out, min_out, latency_ms)
