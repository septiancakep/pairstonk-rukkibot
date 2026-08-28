"""Deteksi pool CPMM baru dari WebSocket.

Dua mode, dipilih lewat env `DETECTOR`:

* ``tx``   — ``transactionSubscribe`` (WebSocket berbasis Geyser, mis. Helius,
  Triton, QuickNode). Transaksi lengkap ikut dikirim pada commitment
  ``processed``, jadi PoolKeys bisa dirakit tanpa satu pun panggilan RPC
  susulan. Ini jalur cepatnya.
* ``logs`` — ``logsSubscribe``, tersedia di hampir semua RPC. Notifikasi hanya
  berisi signature, jadi masih perlu ``getTransaction`` untuk mengambil daftar
  akun, dan metode itu baru menjawab pada commitment ``confirmed``. Andal, tapi
  menambah ratusan milidetik; pakai untuk uji coba, bukan untuk berlomba.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections import deque
from dataclasses import dataclass
from typing import AsyncIterator

import aiohttp

from .b58 import b58decode
from .codec import PoolKeys, parse_initialize_ix
from .constants import CPMM_PROGRAM_ID, IX_INITIALIZE
from .rpc import RpcPool
from solders.pubkey import Pubkey

log = logging.getLogger("sniper.detect")

_INIT_LOG_MARKER = "Program log: Instruction: Initialize"


@dataclass(slots=True)
class PoolEvent:
    keys: PoolKeys
    signature: str
    detected_at: float
    source: str


def _iter_instructions(tx_json: dict) -> list[dict]:
    """Gabungkan instruksi tingkat atas dan inner instruction.

    Pool CPMM sering dibuat lewat CPI (mis. saat launchpad melakukan migrasi),
    jadi melihat instruksi tingkat atas saja akan melewatkan sebagian pool.
    """
    message = tx_json.get("transaction", {}).get("message", {})
    out: list[dict] = list(message.get("instructions", []))
    for group in (tx_json.get("meta") or {}).get("innerInstructions", []) or []:
        out.extend(group.get("instructions", []))
    return out


def _account_key_table(tx_json: dict) -> list[str]:
    """Daftar akun terurut, termasuk yang datang dari address lookup table."""
    message = tx_json.get("transaction", {}).get("message", {})
    keys: list[str] = []
    for entry in message.get("accountKeys", []):
        keys.append(entry["pubkey"] if isinstance(entry, dict) else entry)
    loaded = (tx_json.get("meta") or {}).get("loadedAddresses") or {}
    keys.extend(loaded.get("writable", []))
    keys.extend(loaded.get("readonly", []))
    return keys


def _resolve_accounts(raw_accounts: list, table: list[str]) -> list[Pubkey] | None:
    """Instruksi bisa membawa indeks (encoding json) atau pubkey (jsonParsed)."""
    out: list[Pubkey] = []
    for item in raw_accounts:
        if isinstance(item, int):
            if item >= len(table):
                return None
            out.append(Pubkey.from_string(table[item]))
        else:
            out.append(Pubkey.from_string(item))
    return out


def extract_pool_from_transaction(tx_json: dict) -> PoolKeys | None:
    """Cari instruksi `initialize` CPMM di dalam satu transaksi."""
    if (tx_json.get("meta") or {}).get("err") is not None:
        return None
    table = _account_key_table(tx_json)
    cpmm = str(CPMM_PROGRAM_ID)

    for ix in _iter_instructions(tx_json):
        program_id = ix.get("programId")
        if program_id is None:
            index = ix.get("programIdIndex")
            program_id = table[index] if index is not None and index < len(table) else None
        if program_id != cpmm:
            continue

        raw_data = ix.get("data")
        if not raw_data:
            continue
        try:
            data = b58decode(raw_data)
        except ValueError:
            continue
        if data[:8] != IX_INITIALIZE:
            continue

        accounts = _resolve_accounts(ix.get("accounts", []), table)
        if accounts is None:
            continue
        keys = parse_initialize_ix(data, accounts)
        if keys is not None:
            return keys
    return None


class _RecentSignatures:
    """Penyaring duplikat berukuran tetap.

    Satu transaksi bisa terlihat dua kali (siaran ulang, reconnect), dan set
    yang tumbuh tanpa batas akan bocor pada proses yang hidup berhari-hari.
    """

    def __init__(self, capacity: int = 4096) -> None:
        self._capacity = capacity
        self._order: deque[str] = deque()
        self._members: set[str] = set()

    def add_if_new(self, signature: str) -> bool:
        if signature in self._members:
            return False
        self._members.add(signature)
        self._order.append(signature)
        if len(self._order) > self._capacity:
            self._members.discard(self._order.popleft())
        return True


class Detector:
    """Langganan WebSocket dengan reconnect otomatis."""

    def __init__(
        self,
        ws_url: str,
        rpc: RpcPool,
        mode: str = "logs",
        *,
        commitment: str = "processed",
    ) -> None:
        self._ws_url = ws_url
        self._rpc = rpc
        self._mode = mode if mode in {"tx", "logs"} else "logs"
        self._commitment = commitment
        self._seen = _RecentSignatures()
        self._out: asyncio.Queue[PoolEvent] = asyncio.Queue()
        self._workers: set[asyncio.Task[None]] = set()

    def _subscribe_payload(self) -> dict:
        if self._mode == "tx":
            return {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "transactionSubscribe",
                "params": [
                    {
                        "accountInclude": [str(CPMM_PROGRAM_ID)],
                        "failed": False,
                        "vote": False,
                    },
                    {
                        "commitment": self._commitment,
                        "encoding": "jsonParsed",
                        "transactionDetails": "full",
                        "showRewards": False,
                        "maxSupportedTransactionVersion": 0,
                    },
                ],
            }
        return {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "logsSubscribe",
            "params": [
                {"mentions": [str(CPMM_PROGRAM_ID)]},
                {"commitment": self._commitment},
            ],
        }

    async def stream(self) -> AsyncIterator[PoolEvent]:
        """Aliran pool baru. Pembacaan socket berjalan di task terpisah supaya
        pemrosesan satu notifikasi tidak pernah menahan notifikasi berikutnya."""
        reader = asyncio.create_task(self._read_forever(), name="detector-reader")
        try:
            while True:
                yield await self._out.get()
        finally:
            reader.cancel()
            for worker in list(self._workers):
                worker.cancel()

    async def _read_forever(self) -> None:
        backoff = 1.0
        while True:
            try:
                await self._read_once()
                backoff = 1.0
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - detektor tidak boleh mati
                log.warning("koneksi WebSocket putus (%s), sambung ulang %.0fs", exc, backoff)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30.0)

    async def _read_once(self) -> None:
        async with self._rpc.session.ws_connect(
            self._ws_url, heartbeat=20, max_msg_size=32 * 1024 * 1024
        ) as ws:
            await ws.send_json(self._subscribe_payload())
            log.info("berlangganan %s pada program CPMM", self._mode)

            async for message in ws:
                if message.type is not aiohttp.WSMsgType.TEXT:
                    if message.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                        break
                    continue
                payload = json.loads(message.data)
                if "error" in payload:
                    raise RuntimeError(f"RPC menolak langganan: {payload['error']}")
                if "params" not in payload:
                    continue  # konfirmasi langganan
                result = (payload["params"] or {}).get("result") or {}
                self._spawn(result, time.monotonic())

    def _spawn(self, result: dict, detected_at: float) -> None:
        # logsSubscribe membungkus muatan di `value`; transactionSubscribe
        # menaruh signature dan transaksi langsung di `result`.
        value = result.get("value") or result
        signature = value.get("signature") or ""
        if not signature or value.get("err") is not None:
            return
        if not self._seen.add_if_new(signature):
            return
        task = asyncio.create_task(self._handle(value, signature, detected_at))
        self._workers.add(task)
        task.add_done_callback(self._workers.discard)

    async def _handle(self, value: dict, signature: str, detected_at: float) -> None:
        if self._mode == "tx":
            tx_json = value.get("transaction") or value
        else:
            logs = value.get("logs") or []
            if not any(line.startswith(_INIT_LOG_MARKER) for line in logs):
                return
            tx_json = await self._fetch_transaction(signature)
            if tx_json is None:
                log.debug("tidak bisa mengambil transaksi %s", signature)
                return

        try:
            keys = extract_pool_from_transaction(tx_json)
        except Exception as exc:  # noqa: BLE001 - transaksi cacat tidak boleh menjatuhkan detektor
            log.debug("gagal mengurai transaksi %s: %s", signature, exc)
            return
        if keys is not None:
            await self._out.put(PoolEvent(keys, signature, detected_at, self._mode))

    async def _fetch_transaction(self, signature: str, attempts: int = 8) -> dict | None:
        """getTransaction baru menjawab pada commitment confirmed, jadi coba ulang."""
        for attempt in range(attempts):
            try:
                tx_json = await self._rpc.get_transaction(signature)
            except Exception as exc:  # noqa: BLE001
                log.debug("getTransaction gagal: %s", exc)
                tx_json = None
            if tx_json:
                return tx_json
            await asyncio.sleep(0.15 * (attempt + 1))
        return None
