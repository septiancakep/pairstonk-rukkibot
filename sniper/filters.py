"""Filter pra-beli.

Filter dibagi dua: yang murni lokal (gratis, dievaluasi lebih dulu) dan yang
butuh RPC (mahal di jalur panas, jadi opsional lewat CHECK_MINT_AUTHORITY).
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from solders.pubkey import Pubkey

from .codec import PoolKeys, decode_mint
from .config import Config
from .constants import LAMPORTS_PER_SOL, TOKEN_PROGRAM_ID, WSOL_MINT
from .rpc import RpcPool


@dataclass(slots=True)
class Target:
    """Pool yang lolos filter, sudah dinormalkan ke sisi quote/base."""

    keys: PoolKeys
    quote_mint: Pubkey
    base_mint: Pubkey
    quote_side: int
    base_side: int
    quote_reserve: int
    base_reserve: int
    open_time: int

    @property
    def quote_is_sol(self) -> bool:
        return self.quote_mint == WSOL_MINT


class Rejected(Exception):
    """Pool ditolak filter. Pesannya dipakai untuk log dan notifikasi."""


def screen_local(keys: PoolKeys, cfg: Config) -> Target:
    """Semua pemeriksaan yang bisa dijawab dari isi transaksi pembuat pool."""
    quote_side = -1
    for candidate in cfg.quote_mints:
        side = keys.side_of(candidate)
        if side >= 0:
            quote_side = side
            break
    if quote_side < 0:
        raise Rejected("tidak ada quote mint yang diizinkan pada pool ini")

    base_side = 1 - quote_side
    quote_mint, _, quote_program = keys.leg(quote_side)
    base_mint, _, base_program = keys.leg(base_side)

    if str(base_mint) in cfg.blacklist_mints:
        raise Rejected(f"mint masuk daftar hitam: {base_mint}")
    if keys.creator is not None and str(keys.creator) in cfg.blacklist_creators:
        raise Rejected(f"pembuat masuk daftar hitam: {keys.creator}")

    # ATA quote diturunkan dengan program SPL Token klasik di seluruh bot, jadi
    # quote Token-2022 akan menghasilkan alamat yang salah, bukan sekadar risiko.
    if quote_program != TOKEN_PROGRAM_ID:
        raise Rejected("quote mint memakai Token-2022, tidak didukung")

    if not cfg.allow_token_2022 and base_program != TOKEN_PROGRAM_ID:
        raise Rejected(
            "token memakai program Token-2022 (transfer fee / transfer hook bisa "
            "menjebak penjualan); setel ALLOW_TOKEN_2022=true kalau memang mau"
        )

    quote_reserve = keys.init_amount_0 if quote_side == 0 else keys.init_amount_1
    base_reserve = keys.init_amount_1 if quote_side == 0 else keys.init_amount_0
    if base_reserve <= 0 or quote_reserve <= 0:
        raise Rejected("likuiditas awal nol")

    if quote_mint == WSOL_MINT:
        liquidity_sol = quote_reserve / LAMPORTS_PER_SOL
        if liquidity_sol < cfg.min_quote_liquidity_sol:
            raise Rejected(
                f"likuiditas {liquidity_sol:.3f} SOL di bawah minimum "
                f"{cfg.min_quote_liquidity_sol}"
            )
        if liquidity_sol > cfg.max_quote_liquidity_sol:
            raise Rejected(
                f"likuiditas {liquidity_sol:.3f} SOL di atas maksimum "
                f"{cfg.max_quote_liquidity_sol}"
            )
        # Beli lebih besar dari kolamnya sendiri berarti membeli harga kita sendiri.
        if cfg.buy_lamports > quote_reserve // 2:
            raise Rejected(
                f"BUY_SOL_AMOUNT ({cfg.buy_sol} SOL) terlalu besar untuk kolam "
                f"{liquidity_sol:.3f} SOL"
            )

    now = int(time.time())
    if keys.open_time > now + cfg.max_open_delay_sec:
        raise Rejected(
            f"pool baru dibuka {keys.open_time - now}s lagi (batas "
            f"{cfg.max_open_delay_sec}s)"
        )

    return Target(
        keys=keys,
        quote_mint=quote_mint,
        base_mint=base_mint,
        quote_side=quote_side,
        base_side=base_side,
        quote_reserve=quote_reserve,
        base_reserve=base_reserve,
        open_time=keys.open_time,
    )


async def screen_mint_authority(target: Target, rpc: RpcPool) -> None:
    """Tolak token yang masih bisa dicetak atau dibekukan.

    Butuh satu getAccountInfo, jadi menambah latensi. Matikan lewat
    CHECK_MINT_AUTHORITY=false kalau memilih kecepatan di atas penyaringan ini.
    """
    data = await rpc.get_account_data(target.base_mint)
    if data is None:
        raise Rejected("akun mint tidak terbaca")
    info = decode_mint(data)
    if info.has_freeze_authority:
        raise Rejected("mint masih punya freeze authority (token bisa dibekukan)")
    if info.has_mint_authority:
        raise Rejected("mint masih punya mint authority (suplai bisa ditambah)")
