"""Klien JSON-RPC Solana yang ringan dan async.

Sengaja tidak memakai solana-py: yang dibutuhkan hanya beberapa metode, dan
satu `aiohttp.ClientSession` dengan koneksi keep-alive jauh lebih cepat
daripada membangun objek klien per panggilan.
"""

from __future__ import annotations

import asyncio
import base64
import itertools
import logging
from typing import Any

import aiohttp
from solders.pubkey import Pubkey

log = logging.getLogger("sniper.rpc")


class RpcError(RuntimeError):
    pass


class RpcPool:
    """Sekumpulan endpoint HTTP RPC dengan satu session bersama.

    Pembacaan memakai endpoint utama (indeks 0); pengiriman transaksi
    disebar ke semua endpoint sekaligus lewat `broadcast_transaction`.
    """

    def __init__(self, urls: list[str], timeout_sec: float = 10.0) -> None:
        if not urls:
            raise ValueError("minimal satu URL RPC")
        self.urls = urls
        self._ids = itertools.count(1)
        self._timeout = aiohttp.ClientTimeout(total=timeout_sec)
        self._session: aiohttp.ClientSession | None = None

    async def __aenter__(self) -> "RpcPool":
        connector = aiohttp.TCPConnector(limit=64, ttl_dns_cache=300, force_close=False)
        self._session = aiohttp.ClientSession(
            connector=connector,
            timeout=self._timeout,
            headers={"content-type": "application/json"},
        )
        return self

    async def __aexit__(self, *_exc: object) -> None:
        if self._session:
            await self._session.close()

    @property
    def session(self) -> aiohttp.ClientSession:
        if self._session is None:
            raise RuntimeError("RpcPool dipakai di luar 'async with'")
        return self._session

    async def call(self, method: str, params: list[Any], url: str | None = None) -> Any:
        payload = {
            "jsonrpc": "2.0",
            "id": next(self._ids),
            "method": method,
            "params": params,
        }
        async with self.session.post(url or self.urls[0], json=payload) as resp:
            body = await resp.json(content_type=None)
        if "error" in body:
            raise RpcError(f"{method}: {body['error']}")
        return body.get("result")

    # --- pembacaan yang dipakai sniper ------------------------------------

    async def get_account_data(self, pubkey: Pubkey, commitment: str = "processed") -> bytes | None:
        result = await self.call(
            "getAccountInfo",
            [str(pubkey), {"encoding": "base64", "commitment": commitment}],
        )
        value = (result or {}).get("value")
        if not value:
            return None
        return base64.b64decode(value["data"][0])

    async def get_multiple_account_data(
        self, pubkeys: list[Pubkey], commitment: str = "processed"
    ) -> list[bytes | None]:
        result = await self.call(
            "getMultipleAccounts",
            [[str(p) for p in pubkeys], {"encoding": "base64", "commitment": commitment}],
        )
        out: list[bytes | None] = []
        for value in (result or {}).get("value", []):
            out.append(base64.b64decode(value["data"][0]) if value else None)
        return out

    async def get_balance(self, pubkey: Pubkey, commitment: str = "confirmed") -> int:
        result = await self.call("getBalance", [str(pubkey), {"commitment": commitment}])
        return (result or {}).get("value", 0)

    async def get_latest_blockhash(self, commitment: str = "confirmed") -> tuple[str, int]:
        result = await self.call("getLatestBlockhash", [{"commitment": commitment}])
        value = result["value"]
        return value["blockhash"], value["lastValidBlockHeight"]

    async def get_transaction(self, signature: str, commitment: str = "confirmed") -> dict | None:
        return await self.call(
            "getTransaction",
            [
                signature,
                {
                    "encoding": "json",
                    "commitment": commitment,
                    "maxSupportedTransactionVersion": 0,
                },
            ],
        )

    async def get_signature_status(self, signature: str) -> dict | None:
        result = await self.call(
            "getSignatureStatuses", [[signature], {"searchTransactionHistory": False}]
        )
        values = (result or {}).get("value", [])
        return values[0] if values else None

    async def simulate_transaction(self, raw_b64: str) -> dict:
        return await self.call(
            "simulateTransaction",
            [raw_b64, {"encoding": "base64", "commitment": "processed", "sigVerify": False}],
        )

    # --- pengiriman --------------------------------------------------------

    async def send_transaction(self, raw_b64: str, url: str) -> str:
        return await self.call(
            "sendTransaction",
            [
                raw_b64,
                {
                    "encoding": "base64",
                    "skipPreflight": True,
                    "maxRetries": 0,
                    "preflightCommitment": "processed",
                },
            ],
            url=url,
        )

    async def broadcast_transaction(self, raw_b64: str) -> str | None:
        """Kirim transaksi yang sama ke semua endpoint sekaligus.

        Kembali begitu ada satu endpoint yang menerima; sisanya dibiarkan jalan
        di latar belakang supaya transaksi tetap tersebar seluas mungkin tanpa
        menahan pemanggil selama endpoint paling lambat.
        """
        pending = {
            asyncio.create_task(self.send_transaction(raw_b64, url)) for url in self.urls
        }
        try:
            while pending:
                done, pending = await asyncio.wait(
                    pending, return_when=asyncio.FIRST_COMPLETED
                )
                for task in done:
                    exc = task.exception()
                    if exc is None:
                        return task.result()
                    log.debug("sendTransaction gagal di satu endpoint: %s", exc)
            return None
        finally:
            for task in pending:
                task.add_done_callback(_swallow)


def _swallow(task: "asyncio.Task[Any]") -> None:
    """Konsumsi exception dari pengiriman latar belakang agar tidak jadi warning."""
    if not task.cancelled():
        task.exception()
