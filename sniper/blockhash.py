"""Cache blockhash terbaru.

Mengambil blockhash saat pool ketahuan berarti menambah satu round-trip RPC
persis di detik paling mahal. Task ini menyegarkannya di latar belakang supaya
pembangunan transaksi beli tidak menyentuh jaringan sama sekali.
"""

from __future__ import annotations

import asyncio
import logging
import time

from solders.hash import Hash

from .rpc import RpcPool

log = logging.getLogger("sniper.blockhash")


class BlockhashCache:
    def __init__(self, rpc: RpcPool, refresh_ms: int = 500, commitment: str = "confirmed") -> None:
        self._rpc = rpc
        self._refresh = refresh_ms / 1000.0
        self._commitment = commitment
        self._hash: Hash | None = None
        self._last_valid_height = 0
        self._updated_at = 0.0
        self._ready = asyncio.Event()
        self._task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        self._task = asyncio.create_task(self._loop(), name="blockhash-refresh")
        await asyncio.wait_for(self._ready.wait(), timeout=20)

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    async def _loop(self) -> None:
        while True:
            try:
                blockhash, height = await self._rpc.get_latest_blockhash(self._commitment)
                self._hash = Hash.from_string(blockhash)
                self._last_valid_height = height
                self._updated_at = time.monotonic()
                self._ready.set()
            except Exception as exc:  # noqa: BLE001 - loop harus tetap hidup
                log.warning("gagal menyegarkan blockhash: %s", exc)
            await asyncio.sleep(self._refresh)

    @property
    def value(self) -> Hash:
        if self._hash is None:
            raise RuntimeError("blockhash belum tersedia")
        return self._hash

    @property
    def age_seconds(self) -> float:
        return time.monotonic() - self._updated_at if self._updated_at else float("inf")

    @property
    def last_valid_block_height(self) -> int:
        return self._last_valid_height
