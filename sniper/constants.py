"""Alamat program, seed PDA, dan discriminator instruksi Raydium CP-Swap (CPMM).

Discriminator Anchor dihitung saat import (sha256("global:<nama>")[:8]) supaya
tidak ada angka ajaib yang bisa salah ketik.
"""

from __future__ import annotations

import hashlib

from solders.pubkey import Pubkey

# --- Program ---------------------------------------------------------------

CPMM_PROGRAM_ID = Pubkey.from_string("CPMDWBwJDtYax9qW7AyRuVC19Cc4L4Vcy4n2BHAbHkCW")
TOKEN_PROGRAM_ID = Pubkey.from_string("TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA")
TOKEN_2022_PROGRAM_ID = Pubkey.from_string("TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb")
ATA_PROGRAM_ID = Pubkey.from_string("ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL")
SYSTEM_PROGRAM_ID = Pubkey.from_string("11111111111111111111111111111111")

# --- Mint yang sering dipakai sebagai quote --------------------------------

WSOL_MINT = Pubkey.from_string("So11111111111111111111111111111111111111112")
USDC_MINT = Pubkey.from_string("EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v")

# --- PDA otoritas vault CPMM (konstan, dihitung sekali) --------------------

CPMM_AUTH_SEED = b"vault_and_lp_mint_auth_seed"
CPMM_AUTHORITY, CPMM_AUTHORITY_BUMP = Pubkey.find_program_address(
    [CPMM_AUTH_SEED], CPMM_PROGRAM_ID
)


def anchor_discriminator(name: str) -> bytes:
    return hashlib.sha256(f"global:{name}".encode()).digest()[:8]


IX_INITIALIZE = anchor_discriminator("initialize")
IX_SWAP_BASE_INPUT = anchor_discriminator("swap_base_input")
IX_SWAP_BASE_OUTPUT = anchor_discriminator("swap_base_output")

# Penyebut fee CP-Swap: trade_fee_rate dinyatakan per sejuta.
FEE_RATE_DENOMINATOR = 1_000_000
# Fee default kalau amm_config belum sempat dibaca (0.25%).
DEFAULT_TRADE_FEE_RATE = 2_500

# Rent minimum sebuah token account SPL (165 byte). Dipakai untuk estimasi biaya.
TOKEN_ACCOUNT_RENT_LAMPORTS = 2_039_280
LAMPORTS_PER_SOL = 1_000_000_000

# Tip account Jito mainnet. Dipakai sebagai fallback kalau getTipAccounts gagal.
JITO_TIP_ACCOUNTS_FALLBACK = [
    "96gYZGLnJYVFmbjzopPSU6QiEV5fGqZNyN9nmNhvrZU5",
    "HFqU5x63VTqvQss8hp11i4wVV8bD44PvwucfZ2bU7gRe",
    "Cw8CFyM9FkoMi7K7Crf6HNQqf4uEMzpKw6QNghXLvLkY",
    "ADaUMid9yfUytqMBgopwjb2DTLSokTSzL1zt6iGPaS49",
    "DfXygSm4jCyNCybVYYK6DwvWqjKee8pbDmJGcLWNDXjh",
    "ADuUkR4vqLUMWXxW9gh6D6L8pMSawimctcNZ5pGwDcEt",
    "DttWaMuVvTiduZRnguLF7jNxTgiMBZ1hyAumKUiL2KRL",
    "3AVi9Tg9Uo68tJfuvoKvqKNWKkC5wPdSSdeBnizKZ6jT",
]

# Urutan akun instruksi `initialize` CP-Swap. Indeks ini yang dipakai untuk
# menambang semua alamat pool dari transaksi pembuat pool — tanpa satu pun
# panggilan RPC tambahan.
INIT_ACC_CREATOR = 0
INIT_ACC_AMM_CONFIG = 1
INIT_ACC_AUTHORITY = 2
INIT_ACC_POOL_STATE = 3
INIT_ACC_TOKEN_0_MINT = 4
INIT_ACC_TOKEN_1_MINT = 5
INIT_ACC_LP_MINT = 6
INIT_ACC_TOKEN_0_VAULT = 10
INIT_ACC_TOKEN_1_VAULT = 11
INIT_ACC_OBSERVATION = 13
INIT_ACC_TOKEN_0_PROGRAM = 15
INIT_ACC_TOKEN_1_PROGRAM = 16
INIT_ACC_MIN_LEN = 20
