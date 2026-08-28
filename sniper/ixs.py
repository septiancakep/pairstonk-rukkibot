"""Pembangun instruksi: ATA, SPL Token, dan swap Raydium CP-Swap."""

from __future__ import annotations

import struct

from solders.instruction import AccountMeta, Instruction
from solders.pubkey import Pubkey
from solders.system_program import TransferParams, transfer

from .codec import PoolKeys
from .constants import (
    ATA_PROGRAM_ID,
    CPMM_AUTHORITY,
    CPMM_PROGRAM_ID,
    IX_SWAP_BASE_INPUT,
    SYSTEM_PROGRAM_ID,
    TOKEN_PROGRAM_ID,
    WSOL_MINT,
)


def derive_ata(owner: Pubkey, mint: Pubkey, token_program: Pubkey = TOKEN_PROGRAM_ID) -> Pubkey:
    return Pubkey.find_program_address(
        [bytes(owner), bytes(token_program), bytes(mint)], ATA_PROGRAM_ID
    )[0]


def create_ata_idempotent(
    payer: Pubkey,
    owner: Pubkey,
    mint: Pubkey,
    token_program: Pubkey = TOKEN_PROGRAM_ID,
) -> Instruction:
    """CreateIdempotent — aman dipanggil walau ATA sudah ada, jadi transaksi beli
    tidak perlu cek keberadaan akun lebih dulu."""
    ata = derive_ata(owner, mint, token_program)
    return Instruction(
        program_id=ATA_PROGRAM_ID,
        data=bytes([1]),
        accounts=[
            AccountMeta(payer, True, True),
            AccountMeta(ata, False, True),
            AccountMeta(owner, False, False),
            AccountMeta(mint, False, False),
            AccountMeta(SYSTEM_PROGRAM_ID, False, False),
            AccountMeta(token_program, False, False),
        ],
    )


def sync_native(account: Pubkey, token_program: Pubkey = TOKEN_PROGRAM_ID) -> Instruction:
    return Instruction(
        program_id=token_program,
        data=bytes([17]),
        accounts=[AccountMeta(account, False, True)],
    )


def close_account(
    account: Pubkey,
    destination: Pubkey,
    owner: Pubkey,
    token_program: Pubkey = TOKEN_PROGRAM_ID,
) -> Instruction:
    return Instruction(
        program_id=token_program,
        data=bytes([9]),
        accounts=[
            AccountMeta(account, False, True),
            AccountMeta(destination, False, True),
            AccountMeta(owner, True, False),
        ],
    )


def wrap_sol(owner: Pubkey, lamports: int) -> list[Instruction]:
    """Buat (kalau perlu) WSOL ATA, kirim lamports ke sana, lalu sync_native."""
    ata = derive_ata(owner, WSOL_MINT, TOKEN_PROGRAM_ID)
    return [
        create_ata_idempotent(owner, owner, WSOL_MINT, TOKEN_PROGRAM_ID),
        transfer(TransferParams(from_pubkey=owner, to_pubkey=ata, lamports=lamports)),
        sync_native(ata, TOKEN_PROGRAM_ID),
    ]


def jito_tip(payer: Pubkey, tip_account: Pubkey, lamports: int) -> Instruction:
    return transfer(
        TransferParams(from_pubkey=payer, to_pubkey=tip_account, lamports=lamports)
    )


_SWAP_ARGS = struct.Struct("<QQ")


def swap_base_input(
    payer: Pubkey,
    keys: PoolKeys,
    input_side: int,
    input_token_account: Pubkey,
    output_token_account: Pubkey,
    amount_in: int,
    minimum_amount_out: int,
) -> Instruction:
    """Instruksi `swap_base_input` CP-Swap: belanjakan `amount_in` token masuk.

    `input_side` adalah 0 kalau token masuk adalah token_0 pool, 1 kalau token_1.
    """
    output_side = 1 - input_side
    in_mint, in_vault, in_program = keys.leg(input_side)
    out_mint, out_vault, out_program = keys.leg(output_side)

    return Instruction(
        program_id=CPMM_PROGRAM_ID,
        data=IX_SWAP_BASE_INPUT + _SWAP_ARGS.pack(amount_in, minimum_amount_out),
        accounts=[
            AccountMeta(payer, True, False),
            AccountMeta(CPMM_AUTHORITY, False, False),
            AccountMeta(keys.amm_config, False, False),
            AccountMeta(keys.pool_state, False, True),
            AccountMeta(input_token_account, False, True),
            AccountMeta(output_token_account, False, True),
            AccountMeta(in_vault, False, True),
            AccountMeta(out_vault, False, True),
            AccountMeta(in_program, False, False),
            AccountMeta(out_program, False, False),
            AccountMeta(in_mint, False, False),
            AccountMeta(out_mint, False, False),
            AccountMeta(keys.observation, False, True),
        ],
    )
