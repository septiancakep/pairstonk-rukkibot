"""
StonkFun New Pair Bot
======================
Memantau token baru di StonkFun (stonkfun.xyz, dibangun di atas Raydium)
yang di-pair dengan tokenized stock (xStock/PreStock/dll), lalu mengirim
notifikasi lengkap ke Telegram.

Cara pakai:
    1. pip install -r requirements.txt
    2. Salin .env.example -> .env, isi TELEGRAM_BOT_TOKEN
    3. python bot.py
    4. Di Telegram, chat bot lalu kirim /start untuk berlangganan

Data diambil dari API publik StonkFun (tanpa API key):
    https://www.stonkfun.xyz/api/public/v1/tokens
"""

import asyncio
import json
import logging
import os
from pathlib import Path

import requests
from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import Application, CommandHandler, ContextTypes

# --------------------------------------------------------------------------
# Konfigurasi
# --------------------------------------------------------------------------

BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
POLL_INTERVAL_SECONDS = int(os.environ.get("POLL_INTERVAL_SECONDS", "20"))
API_BASE = "https://www.stonkfun.xyz/api/public/v1"

# Kategori pair yang dianggap "stock". Bisa diubah lewat env var, pisahkan
# dengan koma. Kategori yang tersedia di StonkFun antara lain:
#   xstock   -> tokenized public stocks (xStocks, mis. NVDAX, SPYX)
#   prestock -> tokenized pre-IPO shares (PreStocks)
#   sunrise  -> kategori lain (mis. index/backpack)
#   custom, currencies, leverage, dst -> bukan stock
STOCK_CATEGORIES = set(
    c.strip().lower()
    for c in os.environ.get("STOCK_CATEGORIES", "xstock,prestock").split(",")
    if c.strip()
)

MIN_MARKET_CAP_USD = float(os.environ.get("MIN_MARKET_CAP_USD", "0"))

DATA_DIR = Path(__file__).parent / "data"
DATA_DIR.mkdir(exist_ok=True)
STATE_FILE = DATA_DIR / "state.json"

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("stonkfun-bot")


# --------------------------------------------------------------------------
# State persisten (subscriber & timestamp terakhir yang sudah dikirim)
# --------------------------------------------------------------------------

def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except json.JSONDecodeError:
            logger.warning("state.json korup, membuat baru")
    return {"subscribers": [], "last_seen": None}


def save_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state, indent=2))


# --------------------------------------------------------------------------
# Ambil data dari StonkFun API
# --------------------------------------------------------------------------

def fetch_newest_tokens(page_size: int = 50) -> list[dict]:
    url = f"{API_BASE}/tokens"
    params = {"sort": "newest", "pageSize": page_size}
    resp = requests.get(url, params=params, timeout=15)
    resp.raise_for_status()
    payload = resp.json()
    return payload["data"]["tokens"]


def format_pair_message(token: dict) -> str:
    quote = token.get("quote", {})
    market = token.get("market", {})

    name = token.get("name", "?")
    symbol = token.get("symbol", "?")
    mint = token.get("mint", "")
    quote_symbol = quote.get("symbol", "?")
    quote_name = quote.get("name", "?")
    category = quote.get("categoryLabel", quote.get("category", "?"))
    mode = token.get("mode", "?")

    mcap = market.get("marketCapUsd", 0) or 0
    vol24h = market.get("volume24hUsd", 0) or 0
    price = market.get("priceUsd", 0) or 0
    progress = (token.get("graduationProgress", 0) or 0) * 100

    token_url = f"https://www.stonkfun.xyz/token/{mint}"
    solscan_url = f"https://solscan.io/token/{mint}"

    lines = [
        f"🆕 <b>Pair Baru: {symbol}</b> / {quote_symbol}",
        f"",
        f"• Token: {name} (${symbol})",
        f"• Dipasangkan dengan: {quote_name} (${quote_symbol}) — <i>{category}</i>",
        f"• Mode: {mode}",
        f"• Harga: ${price:.8f}",
        f"• Market Cap: ${mcap:,.0f}",
        f"• Volume 24h: ${vol24h:,.0f}",
        f"• Progress graduasi: {progress:.1f}%",
        f"",
        f"Mint: <code>{mint}</code>",
        f'<a href="{token_url}">Buka di StonkFun</a> | <a href="{solscan_url}">Solscan</a>',
    ]
    return "\n".join(lines)


# --------------------------------------------------------------------------
# Job periodik: cek pair baru & broadcast
# --------------------------------------------------------------------------

async def check_new_pairs(context: ContextTypes.DEFAULT_TYPE) -> None:
    state = load_state()
    subscribers = state.get("subscribers", [])

    try:
        tokens = await asyncio.to_thread(fetch_newest_tokens, 50)
    except Exception as exc:
        logger.error("Gagal fetch API StonkFun: %s", exc)
        return

    last_seen = state.get("last_seen")

    # Filter hanya pair "stock" sesuai STOCK_CATEGORIES, lalu urutkan lama->baru
    candidates = [
        t for t in tokens
        if t.get("quote", {}).get("category", "").lower() in STOCK_CATEGORIES
        and (t.get("market", {}).get("marketCapUsd", 0) or 0) >= MIN_MARKET_CAP_USD
    ]
    candidates.sort(key=lambda t: t.get("createdAt", ""))

    if last_seen is None:
        # Run pertama kali: jangan spam histori, cukup catat titik awal.
        if candidates:
            state["last_seen"] = candidates[-1]["createdAt"]
            save_state(state)
        logger.info("Inisialisasi last_seen, %d token stock terpantau (tidak dikirim).", len(candidates))
        return

    new_tokens = [t for t in candidates if t.get("createdAt", "") > last_seen]

    if not new_tokens:
        return

    logger.info("Menemukan %d pair stock baru", len(new_tokens))

    for token in new_tokens:
        message = format_pair_message(token)
        for chat_id in subscribers:
            try:
                await context.bot.send_message(
                    chat_id=chat_id,
                    text=message,
                    parse_mode=ParseMode.HTML,
                    disable_web_page_preview=True,
                )
            except Exception as exc:
                logger.warning("Gagal kirim ke chat %s: %s", chat_id, exc)

    state["last_seen"] = new_tokens[-1]["createdAt"]
    save_state(state)


# --------------------------------------------------------------------------
# Command handlers
# --------------------------------------------------------------------------

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    state = load_state()
    chat_id = update.effective_chat.id
    if chat_id not in state["subscribers"]:
        state["subscribers"].append(chat_id)
        save_state(state)
    await update.message.reply_text(
        "✅ Berlangganan aktif!\n"
        f"Kamu akan menerima notifikasi pair baru StonkFun "
        f"(kategori: {', '.join(sorted(STOCK_CATEGORIES))}) setiap kali muncul.\n\n"
        "Perintah lain:\n"
        "/stop - berhenti berlangganan\n"
        "/status - cek status bot"
    )


async def cmd_stop(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    state = load_state()
    chat_id = update.effective_chat.id
    if chat_id in state["subscribers"]:
        state["subscribers"].remove(chat_id)
        save_state(state)
    await update.message.reply_text("🛑 Berhenti berlangganan notifikasi.")


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    state = load_state()
    subscribed = update.effective_chat.id in state["subscribers"]
    await update.message.reply_text(
        f"Status langganan: {'AKTIF ✅' if subscribed else 'nonaktif'}\n"
        f"Total subscriber bot: {len(state['subscribers'])}\n"
        f"Interval cek: {POLL_INTERVAL_SECONDS} detik\n"
        f"Kategori dipantau: {', '.join(sorted(STOCK_CATEGORIES))}\n"
        f"Min market cap filter: ${MIN_MARKET_CAP_USD:,.0f}"
    )


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------

def main() -> None:
    if not BOT_TOKEN:
        raise SystemExit(
            "TELEGRAM_BOT_TOKEN belum diset. Isi file .env atau export env var."
        )

    application = Application.builder().token(BOT_TOKEN).build()

    application.add_handler(CommandHandler("start", cmd_start))
    application.add_handler(CommandHandler("stop", cmd_stop))
    application.add_handler(CommandHandler("status", cmd_status))

    application.job_queue.run_repeating(
        check_new_pairs, interval=POLL_INTERVAL_SECONDS, first=5
    )

    logger.info("Bot berjalan. Polling StonkFun setiap %s detik.", POLL_INTERVAL_SECONDS)
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
