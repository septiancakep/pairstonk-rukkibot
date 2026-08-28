"""Encoder/decoder biner untuk akun & instruksi yang dipakai sniper.

Semua di-parse manual (struct + memoryview) supaya tidak perlu Anchor/Borsh
runtime dan tidak ada alokasi berlebih di jalur panas.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass

from solders.pubkey import Pubkey

from .constants import (
    DEFAULT_TRADE_FEE_RATE,
    FEE_RATE_DENOMINATOR,
    INIT_ACC_AMM_CONFIG,
    INIT_ACC_CREATOR,
    INIT_ACC_MIN_LEN,
    INIT_ACC_OBSERVATION,
    INIT_ACC_POOL_STATE,
    INIT_ACC_TOKEN_0_MINT,
    INIT_ACC_TOKEN_0_PROGRAM,
    INIT_ACC_TOKEN_0_VAULT,
    INIT_ACC_TOKEN_1_MINT,
    INIT_ACC_TOKEN_1_PROGRAM,
    INIT_ACC_TOKEN_1_VAULT,
    IX_INITIALIZE,
)

_U64 = struct.Struct("<Q")


def _pubkey_at(buf: bytes, offset: int) -> Pubkey:
    return Pubkey.from_bytes(buf[offset : offset + 32])


def _u64_at(buf: bytes, offset: int) -> int:
    return _U64.unpack_from(buf, offset)[0]


# ---------------------------------------------------------------------------
# Pool CPMM
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class PoolKeys:
    """Semua alamat yang dibutuhkan untuk swap pada satu pool CPMM."""

    pool_state: Pubkey
    amm_config: Pubkey
    observation: Pubkey
    token_0_mint: Pubkey
    token_1_mint: Pubkey
    token_0_vault: Pubkey
    token_1_vault: Pubkey
    token_0_program: Pubkey
    token_1_program: Pubkey
    creator: Pubkey | None = None
    init_amount_0: int = 0
    init_amount_1: int = 0
    open_time: int = 0

    def side_of(self, mint: Pubkey) -> int:
        """0 kalau `mint` adalah token_0, 1 kalau token_1, -1 kalau bukan keduanya."""
        if mint == self.token_0_mint:
            return 0
        if mint == self.token_1_mint:
            return 1
        return -1

    def leg(self, side: int) -> tuple[Pubkey, Pubkey, Pubkey]:
        """(mint, vault, token_program) untuk sisi 0/1."""
        if side == 0:
            return self.token_0_mint, self.token_0_vault, self.token_0_program
        return self.token_1_mint, self.token_1_vault, self.token_1_program


# Offset field PoolState (setelah 8 byte discriminator Anchor).
_PS_AMM_CONFIG = 8
_PS_CREATOR = 40
_PS_VAULT_0 = 72
_PS_VAULT_1 = 104
_PS_LP_MINT = 136
_PS_MINT_0 = 168
_PS_MINT_1 = 200
_PS_PROGRAM_0 = 232
_PS_PROGRAM_1 = 264
_PS_OBSERVATION = 296
_PS_STATUS = 329
_PS_DECIMALS_0 = 331
_PS_DECIMALS_1 = 332
_PS_PROTOCOL_FEE_0 = 341
_PS_PROTOCOL_FEE_1 = 349
_PS_FUND_FEE_0 = 357
_PS_FUND_FEE_1 = 365
_PS_OPEN_TIME = 373
POOL_STATE_MIN_LEN = 381


@dataclass(slots=True)
class PoolState:
    keys: PoolKeys
    status: int
    decimals_0: int
    decimals_1: int
    protocol_fees_0: int
    protocol_fees_1: int
    fund_fees_0: int
    fund_fees_1: int
    open_time: int

    def fees_for(self, side: int) -> int:
        if side == 0:
            return self.protocol_fees_0 + self.fund_fees_0
        return self.protocol_fees_1 + self.fund_fees_1


def decode_pool_state(pool_state_addr: Pubkey, data: bytes) -> PoolState:
    if len(data) < POOL_STATE_MIN_LEN:
        raise ValueError(f"data PoolState terlalu pendek: {len(data)}")
    keys = PoolKeys(
        pool_state=pool_state_addr,
        amm_config=_pubkey_at(data, _PS_AMM_CONFIG),
        observation=_pubkey_at(data, _PS_OBSERVATION),
        token_0_mint=_pubkey_at(data, _PS_MINT_0),
        token_1_mint=_pubkey_at(data, _PS_MINT_1),
        token_0_vault=_pubkey_at(data, _PS_VAULT_0),
        token_1_vault=_pubkey_at(data, _PS_VAULT_1),
        token_0_program=_pubkey_at(data, _PS_PROGRAM_0),
        token_1_program=_pubkey_at(data, _PS_PROGRAM_1),
        creator=_pubkey_at(data, _PS_CREATOR),
        open_time=_u64_at(data, _PS_OPEN_TIME),
    )
    return PoolState(
        keys=keys,
        status=data[_PS_STATUS],
        decimals_0=data[_PS_DECIMALS_0],
        decimals_1=data[_PS_DECIMALS_1],
        protocol_fees_0=_u64_at(data, _PS_PROTOCOL_FEE_0),
        protocol_fees_1=_u64_at(data, _PS_PROTOCOL_FEE_1),
        fund_fees_0=_u64_at(data, _PS_FUND_FEE_0),
        fund_fees_1=_u64_at(data, _PS_FUND_FEE_1),
        open_time=_u64_at(data, _PS_OPEN_TIME),
    )


# Status pool: bit 0 = deposit disabled, 1 = withdraw disabled, 2 = swap disabled.
def swap_disabled(status: int) -> bool:
    return bool(status & 0b100)


# ---------------------------------------------------------------------------
# AmmConfig (untuk trade_fee_rate)
# ---------------------------------------------------------------------------


def decode_trade_fee_rate(data: bytes) -> int:
    """trade_fee_rate ada di offset 12 (8 disc + bump + flag + index u16)."""
    if len(data) < 20:
        return DEFAULT_TRADE_FEE_RATE
    return _u64_at(data, 12)


# ---------------------------------------------------------------------------
# SPL Token account & mint
# ---------------------------------------------------------------------------

TOKEN_ACCOUNT_AMOUNT_OFFSET = 64


def decode_token_amount(data: bytes) -> int:
    """Ambil field `amount` dari token account SPL (juga valid untuk Token-2022)."""
    if len(data) < TOKEN_ACCOUNT_AMOUNT_OFFSET + 8:
        raise ValueError("data token account terlalu pendek")
    return _u64_at(data, TOKEN_ACCOUNT_AMOUNT_OFFSET)


@dataclass(slots=True)
class MintInfo:
    has_mint_authority: bool
    has_freeze_authority: bool
    supply: int
    decimals: int


def decode_mint(data: bytes) -> MintInfo:
    if len(data) < 82:
        raise ValueError("data mint terlalu pendek")
    return MintInfo(
        has_mint_authority=struct.unpack_from("<I", data, 0)[0] == 1,
        supply=_u64_at(data, 36),
        decimals=data[44],
        has_freeze_authority=struct.unpack_from("<I", data, 46)[0] == 1,
    )


# ---------------------------------------------------------------------------
# Instruksi `initialize` CP-Swap
# ---------------------------------------------------------------------------


def parse_initialize_ix(data: bytes, accounts: list[Pubkey]) -> PoolKeys | None:
    """Ubah satu instruksi CPMM `initialize` menjadi PoolKeys lengkap.

    Mengembalikan None kalau instruksi ini bukan `initialize` atau daftar akunnya
    tidak sesuai. Semua yang dibutuhkan untuk swap ada di sini, jadi jalur beli
    tidak perlu satu pun getAccountInfo.
    """
    if len(data) < 32 or data[:8] != IX_INITIALIZE:
        return None
    if len(accounts) < INIT_ACC_MIN_LEN:
        return None
    init_amount_0, init_amount_1, open_time = struct.unpack_from("<QQQ", data, 8)
    return PoolKeys(
        pool_state=accounts[INIT_ACC_POOL_STATE],
        amm_config=accounts[INIT_ACC_AMM_CONFIG],
        observation=accounts[INIT_ACC_OBSERVATION],
        token_0_mint=accounts[INIT_ACC_TOKEN_0_MINT],
        token_1_mint=accounts[INIT_ACC_TOKEN_1_MINT],
        token_0_vault=accounts[INIT_ACC_TOKEN_0_VAULT],
        token_1_vault=accounts[INIT_ACC_TOKEN_1_VAULT],
        token_0_program=accounts[INIT_ACC_TOKEN_0_PROGRAM],
        token_1_program=accounts[INIT_ACC_TOKEN_1_PROGRAM],
        creator=accounts[INIT_ACC_CREATOR],
        init_amount_0=init_amount_0,
        init_amount_1=init_amount_1,
        open_time=open_time,
    )


# ---------------------------------------------------------------------------
# Matematika constant-product CP-Swap
# ---------------------------------------------------------------------------


def swap_base_input_output(
    amount_in: int, reserve_in: int, reserve_out: int, trade_fee_rate: int
) -> int:
    """Hitung keluaran `swap_base_input`, meniru perhitungan integer on-chain.

    Fee dibulatkan ke atas lalu dipotong dari input; sisanya masuk kurva x*y=k
    dengan pembagian dibulatkan ke bawah.
    """
    if amount_in <= 0 or reserve_in <= 0 or reserve_out <= 0:
        return 0
    fee = -(-amount_in * trade_fee_rate // FEE_RATE_DENOMINATOR)  # ceil div
    amount_in_less_fee = amount_in - fee
    if amount_in_less_fee <= 0:
        return 0
    return (reserve_out * amount_in_less_fee) // (reserve_in + amount_in_less_fee)


def apply_slippage(amount: int, slippage_bps: int) -> int:
    """Batas bawah jumlah keluaran yang masih kita terima."""
    return max(0, amount * (10_000 - slippage_bps) // 10_000)
