"""Pengiriman transaksi: siarkan lewat semua RPC dan (opsional) bundle Jito."""

from __future__ import annotations

import asyncio
import base64
import logging
import random
import time

from solders.hash import Hash
from solders.instruction import Instruction
from solders.keypair import Keypair
from solders.message import MessageV0
from solders.pubkey import Pubkey
from solders.transaction import VersionedTransaction

from .constants import JITO_TIP_ACCOUNTS_FALLBACK
from .rpc import RpcPool

log = logging.getLogger("sniper.sender")


class Sender:
    def __init__(
        self,
        rpc: RpcPool,
        *,
        jito_enable: bool = False,
        jito_url: str = "",
        jito_tip_lamports: int = 0,
    ) -> None:
        self._rpc = rpc
        self.jito_enable = jito_enable
        self._jito_url = jito_url.rstrip("/")
        self.jito_tip_lamports = jito_tip_lamports
        self._tip_accounts = [Pubkey.from_string(a) for a in JITO_TIP_ACCOUNTS_FALLBACK]
        self._background: set[asyncio.Task[str | None]] = set()

    def _spawn_background(self, coro) -> None:
        """asyncio hanya memegang weak reference ke task; tanpa set ini, siaran
        ulang bisa dibatalkan GC di tengah jalan."""
        task = asyncio.create_task(coro)
        self._background.add(task)
        task.add_done_callback(self._background.discard)

    async def refresh_jito_tip_accounts(self) -> None:
        """Ambil daftar tip account resmi; kalau gagal, pakai daftar bawaan."""
        if not self.jito_enable:
            return
        try:
            payload = {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "getTipAccounts",
                "params": [],
            }
            async with self._rpc.session.post(
                f"{self._jito_url}/bundles", json=payload
            ) as resp:
                body = await resp.json(content_type=None)
            accounts = body.get("result") or []
            if accounts:
                self._tip_accounts = [Pubkey.from_string(a) for a in accounts]
                log.info("Jito: %d tip account dimuat", len(self._tip_accounts))
        except Exception as exc:  # noqa: BLE001 - daftar fallback tetap valid
            log.warning("Jito getTipAccounts gagal, pakai daftar bawaan: %s", exc)

    def random_tip_account(self) -> Pubkey:
        return random.choice(self._tip_accounts)

    async def send_bundle(self, raw_b64_list: list[str]) -> str | None:
        payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "sendBundle",
            "params": [raw_b64_list, {"encoding": "base64"}],
        }
        async with self._rpc.session.post(f"{self._jito_url}/bundles", json=payload) as resp:
            body = await resp.json(content_type=None)
        if "error" in body:
            raise RuntimeError(f"Jito sendBundle: {body['error']}")
        return body.get("result")

    async def send(self, tx: VersionedTransaction) -> str | None:
        """Siarkan transaksi. Kalau Jito aktif, bundle dan RPC dijalankan paralel.

        Mengirim lewat dua jalur sekaligus tidak menggandakan eksekusi: keduanya
        membawa transaksi bertanda tangan yang sama, jadi hanya satu yang bisa
        masuk ledger.
        """
        raw_b64 = base64.b64encode(bytes(tx)).decode()
        signature = str(tx.signatures[0])

        rpc_task = asyncio.create_task(self._rpc.broadcast_transaction(raw_b64))
        if self.jito_enable:
            try:
                await self.send_bundle([raw_b64])
            except Exception as exc:  # noqa: BLE001 - RPC biasa masih jalan
                log.warning("pengiriman bundle Jito gagal: %s", exc)
        try:
            await rpc_task
        except Exception as exc:  # noqa: BLE001
            log.warning("siaran RPC gagal: %s", exc)
        return signature

    async def rebroadcast_until_confirmed(
        self,
        tx: VersionedTransaction,
        *,
        timeout_sec: float = 30.0,
        interval_sec: float = 1.0,
    ) -> tuple[str, bool, str | None]:
        """Kirim ulang berkala sampai transaksi terkonfirmasi atau waktu habis.

        Mengembalikan (signature, sukses, pesan_error). Pengiriman ulang aman
        karena signature-nya identik — validator akan menolak duplikat.
        """
        raw_b64 = base64.b64encode(bytes(tx)).decode()
        signature = str(tx.signatures[0])
        deadline = time.monotonic() + timeout_sec

        await self.send(tx)
        while time.monotonic() < deadline:
            await asyncio.sleep(interval_sec)
            try:
                status = await self._rpc.get_signature_status(signature)
            except Exception as exc:  # noqa: BLE001
                log.debug("cek status gagal: %s", exc)
                status = None
            if status:
                if status.get("err"):
                    return signature, False, str(status["err"])
                if status.get("confirmationStatus") in {"confirmed", "finalized"}:
                    return signature, True, None
            self._spawn_background(self._rpc.broadcast_transaction(raw_b64))
        return signature, False, "timeout menunggu konfirmasi"


def build_transaction(
    payer: Keypair, instructions: list[Instruction], blockhash: Hash
) -> VersionedTransaction:
    message = MessageV0.try_compile(payer.pubkey(), instructions, [], blockhash)
    return VersionedTransaction(message, [payer])
