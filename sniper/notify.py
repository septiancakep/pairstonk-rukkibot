"""Notifikasi Telegram opsional. Kegagalan kirim tidak pernah menghentikan bot."""

from __future__ import annotations

import asyncio
import logging

from .rpc import RpcPool

log = logging.getLogger("sniper.notify")


class Notifier:
    def __init__(self, rpc: RpcPool, bot_token: str, chat_id: str) -> None:
        self._rpc = rpc
        self._token = bot_token
        self._chat_id = chat_id

    @property
    def enabled(self) -> bool:
        return bool(self._token and self._chat_id)

    async def send(self, text: str) -> None:
        if not self.enabled:
            return
        url = f"https://api.telegram.org/bot{self._token}/sendMessage"
        payload = {
            "chat_id": self._chat_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }
        try:
            async with self._rpc.session.post(url, json=payload) as resp:
                if resp.status != 200:
                    log.warning("Telegram menolak pesan: HTTP %s", resp.status)
        except Exception as exc:  # noqa: BLE001 - notifikasi tidak boleh fatal
            log.warning("gagal mengirim notifikasi: %s", exc)

    def send_soon(self, text: str) -> None:
        """Kirim tanpa menahan jalur eksekusi."""
        if self.enabled:
            asyncio.create_task(self.send(text))
