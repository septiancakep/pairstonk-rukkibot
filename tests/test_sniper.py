"""Tes offline: seluruh jalur dari notifikasi WebSocket sampai transaksi beli
yang sudah ditandatangani, tanpa menyentuh jaringan."""

from __future__ import annotations

import struct
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from solders.keypair import Keypair
from solders.pubkey import Pubkey

from sniper.b58 import b58decode, b58encode
from sniper.codec import (
    apply_slippage,
    decode_mint,
    decode_pool_state,
    decode_token_amount,
    decode_trade_fee_rate,
    parse_initialize_ix,
    swap_base_input_output,
)
from sniper.config import Config, parse_keypair, parse_ladder
from sniper.constants import (
    CPMM_AUTHORITY,
    CPMM_PROGRAM_ID,
    IX_INITIALIZE,
    IX_SWAP_BASE_INPUT,
    TOKEN_PROGRAM_ID,
    WSOL_MINT,
)
from sniper.detect import extract_pool_from_transaction
from sniper.filters import Rejected, screen_local
from sniper.ixs import create_ata_idempotent, derive_ata, swap_base_input
from sniper.sender import build_transaction

TOKEN_MINT = Pubkey.from_string("6p6xgHyF7AeE6TZkSmFsko444wqoP15icUSqi2jfGiPN")
PAYER = Keypair()


def _fake_accounts() -> list[Pubkey]:
    """20 akun instruksi `initialize`, dengan slot penting diisi nilai kenalan."""
    accounts = [Pubkey.new_unique() for _ in range(20)]
    accounts[2] = CPMM_AUTHORITY
    accounts[4] = WSOL_MINT  # token_0 = quote
    accounts[5] = TOKEN_MINT  # token_1 = base
    accounts[15] = TOKEN_PROGRAM_ID
    accounts[16] = TOKEN_PROGRAM_ID
    return accounts


def _init_ix_data(amount_0: int, amount_1: int, open_time: int = 0) -> bytes:
    return IX_INITIALIZE + struct.pack("<QQQ", amount_0, amount_1, open_time)


def _fake_tx_json(accounts: list[Pubkey], data: bytes, inner: bool = False) -> dict:
    ix = {
        "programId": str(CPMM_PROGRAM_ID),
        "accounts": [str(a) for a in accounts],
        "data": b58encode(data),
    }
    top = [] if inner else [ix]
    meta = {"err": None, "innerInstructions": [{"index": 0, "instructions": [ix]}] if inner else []}
    return {"transaction": {"message": {"accountKeys": [], "instructions": top}}, "meta": meta}


def _config(**overrides) -> Config:
    base = dict(
        keypair=PAYER,
        rpc_http_urls=["http://localhost:8899"],
        rpc_ws_url="ws://localhost:8900",
        buy_sol=0.05,
        min_quote_liquidity_sol=0.5,
        max_quote_liquidity_sol=500.0,
        dry_run=True,
    )
    base.update(overrides)
    return Config(**base)


class TestBase58(unittest.TestCase):
    def test_roundtrip_matches_solders(self):
        for _ in range(50):
            key = Pubkey.new_unique()
            self.assertEqual(b58decode(str(key)), bytes(key))
            self.assertEqual(b58encode(bytes(key)), str(key))

    def test_leading_zero_bytes_preserved(self):
        self.assertEqual(b58decode("111"), b"\x00\x00\x00")
        self.assertEqual(b58encode(b"\x00\x00\x01"), "112")


class TestSwapMath(unittest.TestCase):
    def test_constant_product_with_fee(self):
        # Kolam 100 SOL / 1_000_000 token, masuk 1 SOL, fee 0.25%.
        out = swap_base_input_output(10**9, 100 * 10**9, 10**15, 2_500)
        # Tanpa fee hasilnya ~9.9009e12; fee memotongnya sedikit di bawah itu.
        self.assertLess(out, 10**15 * 10**9 // (100 * 10**9 + 10**9))
        self.assertGreater(out, 9.8e12)

    def test_fee_rounds_up_against_trader(self):
        # amount_in kecil: fee dibulatkan ke atas, bukan ke bawah.
        no_fee = swap_base_input_output(1_000, 10**9, 10**9, 0)
        with_fee = swap_base_input_output(1_000, 10**9, 10**9, 1)
        self.assertLess(with_fee, no_fee)

    def test_degenerate_inputs_return_zero(self):
        self.assertEqual(swap_base_input_output(0, 10, 10, 0), 0)
        self.assertEqual(swap_base_input_output(10, 0, 10, 0), 0)
        self.assertEqual(swap_base_input_output(10, 10, 0, 0), 0)
        # Fee menelan seluruh input.
        self.assertEqual(swap_base_input_output(1, 10**9, 10**9, 1_000_000), 0)

    def test_slippage_floor(self):
        self.assertEqual(apply_slippage(1_000, 1_500), 850)
        self.assertEqual(apply_slippage(1_000, 10_000), 0)


class TestInitializeParsing(unittest.TestCase):
    def test_parses_all_pool_keys(self):
        accounts = _fake_accounts()
        keys = parse_initialize_ix(_init_ix_data(3 * 10**9, 10**15, 1234), accounts)
        assert keys is not None
        self.assertEqual(keys.pool_state, accounts[3])
        self.assertEqual(keys.amm_config, accounts[1])
        self.assertEqual(keys.observation, accounts[13])
        self.assertEqual(keys.token_0_vault, accounts[10])
        self.assertEqual(keys.token_1_vault, accounts[11])
        self.assertEqual(keys.token_0_mint, WSOL_MINT)
        self.assertEqual(keys.token_1_mint, TOKEN_MINT)
        self.assertEqual(keys.creator, accounts[0])
        self.assertEqual(keys.init_amount_0, 3 * 10**9)
        self.assertEqual(keys.init_amount_1, 10**15)
        self.assertEqual(keys.open_time, 1234)

    def test_rejects_other_instructions(self):
        accounts = _fake_accounts()
        self.assertIsNone(parse_initialize_ix(IX_SWAP_BASE_INPUT + b"\x00" * 24, accounts))
        self.assertIsNone(parse_initialize_ix(_init_ix_data(1, 1), accounts[:10]))

    def test_side_lookup(self):
        keys = parse_initialize_ix(_init_ix_data(1, 1), _fake_accounts())
        assert keys is not None
        self.assertEqual(keys.side_of(WSOL_MINT), 0)
        self.assertEqual(keys.side_of(TOKEN_MINT), 1)
        self.assertEqual(keys.side_of(Pubkey.new_unique()), -1)


class TestDetectorExtraction(unittest.TestCase):
    def test_extracts_from_top_level_instruction(self):
        accounts = _fake_accounts()
        tx = _fake_tx_json(accounts, _init_ix_data(2 * 10**9, 10**15))
        keys = extract_pool_from_transaction(tx)
        assert keys is not None
        self.assertEqual(keys.pool_state, accounts[3])

    def test_extracts_from_inner_instruction(self):
        # Pool yang lahir dari migrasi launchpad muncul sebagai CPI, bukan
        # instruksi tingkat atas.
        accounts = _fake_accounts()
        tx = _fake_tx_json(accounts, _init_ix_data(2 * 10**9, 10**15), inner=True)
        keys = extract_pool_from_transaction(tx)
        assert keys is not None
        self.assertEqual(keys.pool_state, accounts[3])

    def test_resolves_indexed_accounts_and_lookup_tables(self):
        accounts = _fake_accounts()
        static = [str(a) for a in accounts[:12]]
        loaded = [str(a) for a in accounts[12:]]
        tx = {
            "transaction": {
                "message": {
                    "accountKeys": [{"pubkey": k} for k in static] + [{"pubkey": str(CPMM_PROGRAM_ID)}],
                    "instructions": [
                        {
                            "programIdIndex": len(static),
                            "accounts": list(range(20)),
                            "data": b58encode(_init_ix_data(2 * 10**9, 10**15)),
                        }
                    ],
                }
            },
            "meta": {"err": None, "loadedAddresses": {"writable": loaded, "readonly": []}},
        }
        keys = extract_pool_from_transaction(tx)
        assert keys is not None
        self.assertEqual(keys.token_1_mint, TOKEN_MINT)

    def test_ignores_failed_transactions(self):
        tx = _fake_tx_json(_fake_accounts(), _init_ix_data(1, 1))
        tx["meta"]["err"] = {"InstructionError": [0, "Custom"]}
        self.assertIsNone(extract_pool_from_transaction(tx))

    def test_ignores_other_programs(self):
        tx = _fake_tx_json(_fake_accounts(), _init_ix_data(1, 1))
        tx["transaction"]["message"]["instructions"][0]["programId"] = str(Pubkey.new_unique())
        self.assertIsNone(extract_pool_from_transaction(tx))


class TestFilters(unittest.TestCase):
    def _keys(self, quote_lamports=2 * 10**9, base=10**15, open_time=0, token_program=None):
        accounts = _fake_accounts()
        if token_program is not None:
            accounts[16] = token_program
        return parse_initialize_ix(_init_ix_data(quote_lamports, base, open_time), accounts)

    def test_accepts_healthy_sol_pool(self):
        target = screen_local(self._keys(), _config())
        self.assertEqual(target.quote_mint, WSOL_MINT)
        self.assertEqual(target.base_mint, TOKEN_MINT)
        self.assertEqual(target.quote_side, 0)
        self.assertEqual(target.base_side, 1)
        self.assertEqual(target.quote_reserve, 2 * 10**9)
        self.assertTrue(target.quote_is_sol)

    def test_rejects_liquidity_outside_range(self):
        with self.assertRaises(Rejected):
            screen_local(self._keys(quote_lamports=10**8), _config())
        with self.assertRaises(Rejected):
            screen_local(self._keys(quote_lamports=10**12), _config())

    def test_rejects_buy_larger_than_half_the_pool(self):
        with self.assertRaises(Rejected):
            screen_local(self._keys(quote_lamports=10**9), _config(buy_sol=0.8))

    def test_rejects_token_2022_by_default(self):
        from sniper.constants import TOKEN_2022_PROGRAM_ID

        with self.assertRaises(Rejected):
            screen_local(self._keys(token_program=TOKEN_2022_PROGRAM_ID), _config())
        target = screen_local(
            self._keys(token_program=TOKEN_2022_PROGRAM_ID), _config(allow_token_2022=True)
        )
        self.assertEqual(target.base_mint, TOKEN_MINT)

    def test_rejects_far_future_open_time(self):
        import time as _time

        with self.assertRaises(Rejected):
            screen_local(self._keys(open_time=int(_time.time()) + 3_600), _config())

    def test_rejects_blacklisted_mint(self):
        with self.assertRaises(Rejected):
            screen_local(self._keys(), _config(blacklist_mints={str(TOKEN_MINT)}))

    def test_rejects_token_2022_quote(self):
        from sniper.constants import TOKEN_2022_PROGRAM_ID

        accounts = _fake_accounts()
        accounts[15] = TOKEN_2022_PROGRAM_ID  # program sisi quote
        keys = parse_initialize_ix(_init_ix_data(2 * 10**9, 10**15), accounts)
        with self.assertRaises(Rejected):
            screen_local(keys, _config())

    def test_rejects_pool_without_allowed_quote(self):
        accounts = _fake_accounts()
        accounts[4] = Pubkey.new_unique()
        keys = parse_initialize_ix(_init_ix_data(10**9, 10**15), accounts)
        with self.assertRaises(Rejected):
            screen_local(keys, _config())


class TestExitRules(unittest.TestCase):
    """Aturan keluar posisi murni fungsi dari harga & waktu, jadi bisa diuji
    tanpa jaringan maupun trader."""

    def _manager(self, **overrides):
        from sniper.positions import PositionManager

        return PositionManager(_config(**overrides), None, None)  # type: ignore[arg-type]

    def _position(self, spent=10**8, peak=0, ladder_index=0):
        from sniper.positions import Position

        pos = Position(target=None, tokens_held=1_000, quote_spent=spent)  # type: ignore[arg-type]
        pos.peak_value = peak
        pos.ladder_index = ladder_index
        return pos

    def test_stop_loss_beats_everything(self):
        mgr = self._manager(stop_loss_pct=0.5)
        reason = mgr._exit_reason(self._position(), 4 * 10**7, -0.6, float("inf"))
        self.assertEqual(reason, "stop loss")

    def test_take_profit_uses_current_ladder_step(self):
        mgr = self._manager(take_profit_ladder=[(1.0, 5_000), (3.0, 10_000)])
        pos = self._position()
        self.assertIsNone(mgr._exit_reason(pos, 15 * 10**7, 0.5, float("inf")))
        self.assertEqual(mgr._exit_reason(pos, 2 * 10**8, 1.0, float("inf")), "take profit")
        self.assertEqual(mgr._portion_for(pos, "take profit"), 5_000)

        pos.ladder_index = 1
        # Anak tangga kedua belum tercapai walau anak tangga pertama sudah lewat.
        self.assertIsNone(mgr._exit_reason(pos, 25 * 10**7, 1.5, float("inf")))
        self.assertEqual(mgr._exit_reason(pos, 5 * 10**8, 3.0, float("inf")), "take profit")
        self.assertEqual(mgr._portion_for(pos, "take profit"), 10_000)

        # Tangga habis: kenaikan lebih lanjut tidak memicu apa pun.
        pos.ladder_index = 2
        self.assertIsNone(mgr._exit_reason(pos, 10**9, 9.0, float("inf")))

    def test_trailing_stop_only_after_profit(self):
        mgr = self._manager(trailing_stop_pct=0.3, stop_loss_pct=0.0)
        # Puncak masih di bawah modal -> trailing belum aktif.
        below = self._position(peak=9 * 10**7)
        self.assertIsNone(mgr._exit_reason(below, 5 * 10**7, -0.5, float("inf")))
        # Puncak di atas modal dan harga turun 33% dari puncak -> keluar.
        above = self._position(peak=3 * 10**8)
        self.assertEqual(
            mgr._exit_reason(above, 2 * 10**8, 1.0, float("inf")), "trailing stop"
        )

    def test_hold_timeout_sells_everything(self):
        mgr = self._manager(stop_loss_pct=0.0, take_profit_ladder=[])
        pos = self._position()
        self.assertEqual(mgr._exit_reason(pos, 10**8, 0.0, 0.0), "batas waktu tahan")
        self.assertEqual(mgr._portion_for(pos, "batas waktu tahan"), 10_000)


class TestSwapInstruction(unittest.TestCase):
    def setUp(self):
        self.target = screen_local(
            parse_initialize_ix(_init_ix_data(2 * 10**9, 10**15), _fake_accounts()),
            _config(),
        )

    def test_account_order_and_args(self):
        keys = self.target.keys
        quote_ata = derive_ata(PAYER.pubkey(), WSOL_MINT)
        base_ata = derive_ata(PAYER.pubkey(), TOKEN_MINT)
        ix = swap_base_input(
            PAYER.pubkey(), keys, 0, quote_ata, base_ata, 50_000_000, 123
        )

        self.assertEqual(ix.program_id, CPMM_PROGRAM_ID)
        self.assertEqual(ix.data[:8], IX_SWAP_BASE_INPUT)
        self.assertEqual(struct.unpack("<QQ", ix.data[8:]), (50_000_000, 123))

        metas = ix.accounts
        self.assertEqual(len(metas), 13)
        self.assertEqual(metas[0].pubkey, PAYER.pubkey())
        self.assertTrue(metas[0].is_signer)
        self.assertEqual(metas[1].pubkey, CPMM_AUTHORITY)
        self.assertEqual(metas[2].pubkey, keys.amm_config)
        self.assertEqual(metas[3].pubkey, keys.pool_state)
        self.assertEqual(metas[4].pubkey, quote_ata)
        self.assertEqual(metas[5].pubkey, base_ata)
        self.assertEqual(metas[6].pubkey, keys.token_0_vault)
        self.assertEqual(metas[7].pubkey, keys.token_1_vault)
        self.assertEqual(metas[10].pubkey, WSOL_MINT)
        self.assertEqual(metas[11].pubkey, TOKEN_MINT)
        self.assertEqual(metas[12].pubkey, keys.observation)
        # Pool, kedua vault, kedua token account, dan observation harus writable.
        for index in (3, 4, 5, 6, 7, 12):
            self.assertTrue(metas[index].is_writable, f"akun {index} harus writable")
        for index in (1, 2, 8, 9, 10, 11):
            self.assertFalse(metas[index].is_writable, f"akun {index} tidak boleh writable")

    def test_selling_side_swaps_input_and_output_legs(self):
        keys = self.target.keys
        ix = swap_base_input(
            PAYER.pubkey(), keys, 1,
            derive_ata(PAYER.pubkey(), TOKEN_MINT),
            derive_ata(PAYER.pubkey(), WSOL_MINT),
            1_000, 1,
        )
        self.assertEqual(ix.accounts[6].pubkey, keys.token_1_vault)
        self.assertEqual(ix.accounts[7].pubkey, keys.token_0_vault)
        self.assertEqual(ix.accounts[10].pubkey, TOKEN_MINT)
        self.assertEqual(ix.accounts[11].pubkey, WSOL_MINT)


class TestTransactionAssembly(unittest.TestCase):
    def test_buy_transaction_signs_and_fits_in_a_packet(self):
        from solders.compute_budget import set_compute_unit_limit, set_compute_unit_price
        from solders.hash import Hash

        target = screen_local(
            parse_initialize_ix(_init_ix_data(2 * 10**9, 10**15), _fake_accounts()),
            _config(),
        )
        instructions = [
            set_compute_unit_limit(120_000),
            set_compute_unit_price(500_000),
            create_ata_idempotent(PAYER.pubkey(), PAYER.pubkey(), TOKEN_MINT),
            swap_base_input(
                PAYER.pubkey(), target.keys, 0,
                derive_ata(PAYER.pubkey(), WSOL_MINT),
                derive_ata(PAYER.pubkey(), TOKEN_MINT),
                50_000_000, 1,
            ),
        ]
        tx = build_transaction(PAYER, instructions, Hash.default())
        raw = bytes(tx)
        self.assertLess(len(raw), 1232, "transaksi harus muat dalam satu paket UDP")
        self.assertEqual(len(tx.signatures), 1)
        self.assertEqual(tx.verify_with_results(), [True], "tanda tangan harus valid")


class TestAccountDecoding(unittest.TestCase):
    def test_token_account_amount(self):
        data = bytearray(165)
        struct.pack_into("<Q", data, 64, 987_654_321)
        self.assertEqual(decode_token_amount(bytes(data)), 987_654_321)

    def test_mint_authority_flags(self):
        data = bytearray(82)
        struct.pack_into("<I", data, 0, 1)  # mint authority ada
        struct.pack_into("<Q", data, 36, 1_000)
        data[44] = 9
        struct.pack_into("<I", data, 46, 0)  # freeze authority kosong
        info = decode_mint(bytes(data))
        self.assertTrue(info.has_mint_authority)
        self.assertFalse(info.has_freeze_authority)
        self.assertEqual(info.decimals, 9)
        self.assertEqual(info.supply, 1_000)

    def test_trade_fee_rate_offset(self):
        data = bytearray(108)
        struct.pack_into("<Q", data, 12, 2_500)
        self.assertEqual(decode_trade_fee_rate(bytes(data)), 2_500)

    def test_pool_state_layout(self):
        pool = Pubkey.new_unique()
        data = bytearray(637)
        fields = {
            8: Pubkey.new_unique(),    # amm_config
            40: Pubkey.new_unique(),   # creator
            72: Pubkey.new_unique(),   # vault 0
            104: Pubkey.new_unique(),  # vault 1
            168: WSOL_MINT,            # mint 0
            200: TOKEN_MINT,           # mint 1
            232: TOKEN_PROGRAM_ID,
            264: TOKEN_PROGRAM_ID,
            296: Pubkey.new_unique(),  # observation
        }
        for offset, key in fields.items():
            data[offset : offset + 32] = bytes(key)
        data[329] = 0b100  # swap dimatikan
        data[331] = 9
        data[332] = 6
        struct.pack_into("<Q", data, 341, 11)  # protocol fee 0
        struct.pack_into("<Q", data, 357, 22)  # fund fee 0
        struct.pack_into("<Q", data, 373, 4242)  # open_time

        state = decode_pool_state(pool, bytes(data))
        self.assertEqual(state.keys.amm_config, fields[8])
        self.assertEqual(state.keys.token_0_mint, WSOL_MINT)
        self.assertEqual(state.keys.token_1_mint, TOKEN_MINT)
        self.assertEqual(state.keys.observation, fields[296])
        self.assertEqual(state.decimals_0, 9)
        self.assertEqual(state.decimals_1, 6)
        self.assertEqual(state.fees_for(0), 33)
        self.assertEqual(state.open_time, 4242)

        from sniper.codec import swap_disabled

        self.assertTrue(swap_disabled(state.status))


class TestConfigParsing(unittest.TestCase):
    def test_ladder_parsing_sorts_and_scales(self):
        self.assertEqual(parse_ladder("300:50,100:25"), [(1.0, 2_500), (3.0, 5_000)])
        self.assertEqual(parse_ladder(""), [])

    def test_keypair_accepts_base58_and_json_array(self):
        kp = Keypair()
        from sniper.b58 import b58encode

        self.assertEqual(parse_keypair(b58encode(bytes(kp))).pubkey(), kp.pubkey())
        self.assertEqual(parse_keypair(str(list(bytes(kp)))).pubkey(), kp.pubkey())

    def test_derived_ata_matches_spl_derivation(self):
        owner = PAYER.pubkey()
        expected = Pubkey.find_program_address(
            [bytes(owner), bytes(TOKEN_PROGRAM_ID), bytes(TOKEN_MINT)],
            Pubkey.from_string("ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL"),
        )[0]
        self.assertEqual(derive_ata(owner, TOKEN_MINT), expected)


class TestRateLimiter(unittest.TestCase):
    def test_blocks_beyond_quota(self):
        from sniper.main import RateLimiter

        limiter = RateLimiter(2)
        self.assertTrue(limiter.allow())
        self.assertTrue(limiter.allow())
        self.assertFalse(limiter.allow())


if __name__ == "__main__":
    unittest.main(verbosity=2)
