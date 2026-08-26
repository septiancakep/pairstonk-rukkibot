# StonkFun New Pair Bot

Bot Telegram yang memantau pair token baru di [StonkFun](https://www.stonkfun.xyz)
(platform launchpad di atas Raydium yang men-pair token baru dengan tokenized
stock seperti xStock/PreStock: NVDAX, SPYX, AAPL, dst) dan mengirim notifikasi
lengkap (nama, symbol, quote pair, market cap, volume, progress) ke Telegram.

Data diambil langsung dari API publik StonkFun — tanpa API key, tanpa scraping.

## 1. Buat Bot Telegram

1. Chat [@BotFather](https://t.me/BotFather) di Telegram.
2. Kirim `/newbot`, ikuti instruksinya, kamu akan dapat **token** seperti
   `123456789:AAxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx`.
3. Simpan token itu.

## 2. Setup Lokal (untuk testing)

```bash
git clone <folder-ini>   # atau cukup pakai folder yang sudah ada
cd stonkfun-bot
python -m venv venv
source venv/bin/activate        # Windows: venv\Scripts\activate
pip install -r requirements.txt

cp .env.example .env
# edit .env, isi TELEGRAM_BOT_TOKEN dengan token dari BotFather
```

Jalankan:

```bash
export $(cat .env | xargs)   # load env var (Linux/Mac)
python bot.py
```

Di Windows, cukup set env var manual atau pakai `python-dotenv` (opsional,
tinggal tambahkan `from dotenv import load_dotenv; load_dotenv()` di baris
atas `bot.py` dan `pip install python-dotenv`).

Setelah bot jalan, buka chat bot kamu di Telegram → kirim `/start`.
Bot akan otomatis mendaftarkan chat itu sebagai subscriber dan mulai mengirim
notifikasi pair baru begitu muncul di StonkFun.

## 3. Konfigurasi (opsional)

Semua diatur lewat `.env`:

| Variabel | Default | Keterangan |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | wajib | Token dari BotFather |
| `POLL_INTERVAL_SECONDS` | 20 | Interval cek API (detik) |
| `STOCK_CATEGORIES` | `xstock,prestock` | Kategori pair yang dianggap "stock" |
| `MIN_MARKET_CAP_USD` | 0 | Filter minimum market cap token baru |

Kategori yang tersedia di StonkFun: `xstock` (tokenized public stock),
`prestock` (tokenized pre-IPO), `sunrise`, `custom`, `currencies`, `leverage`,
`backpack`. Ubah `STOCK_CATEGORIES` kalau mau ikut memantau kategori lain.

## 4. Deploy 24/7

Bot ini pakai *long polling* biasa, jadi cukup dijalankan sebagai proses yang
selalu hidup. Beberapa opsi, dari termudah:

### A. VPS murah (disarankan untuk pemakaian serius)
- Provider: Contabo, Vultr, DigitalOcean, atau provider lokal (Biznet, IDCloudHost) — mulai ~$4-6/bulan.
- Setelah VPS aktif (Ubuntu 22.04/24.04):
  ```bash
  sudo apt update && sudo apt install -y python3-venv python3-pip
  # upload folder bot ini via scp/git, lalu:
  cd stonkfun-bot
  python3 -m venv venv && source venv/bin/activate
  pip install -r requirements.txt
  ```
- Jalankan sebagai service systemd biar auto-restart & jalan terus:
  ```ini
  # /etc/systemd/system/stonkfun-bot.service
  [Unit]
  Description=StonkFun Telegram Bot
  After=network.target

  [Service]
  WorkingDirectory=/home/youruser/stonkfun-bot
  EnvironmentFile=/home/youruser/stonkfun-bot/.env
  ExecStart=/home/youruser/stonkfun-bot/venv/bin/python bot.py
  Restart=always
  RestartSec=5

  [Install]
  WantedBy=multi-user.target
  ```
  ```bash
  sudo systemctl daemon-reload
  sudo systemctl enable --now stonkfun-bot
  sudo systemctl status stonkfun-bot   # cek jalan atau tidak
  journalctl -u stonkfun-bot -f        # lihat log realtime
  ```

### B. Railway / Render (paling gampang, ada free tier terbatas)
1. Push folder ini ke repo GitHub.
2. Buat project baru di [Railway](https://railway.app) atau [Render](https://render.com), hubungkan ke repo.
3. Set environment variable `TELEGRAM_BOT_TOKEN` (dan lainnya kalau perlu) di dashboard.
4. Set start command: `python bot.py`.
5. Deploy — platform akan menjaga proses tetap hidup.

### C. Laptop/PC sendiri
Bisa untuk testing, tapi bot akan mati kalau laptop mati/tidur. Untuk pemakaian
serius disarankan pindah ke VPS atau Railway/Render.

## Catatan Teknis

- Bot **tidak** menyimpan private key apa pun — ini murni bot notifikasi baca
  data, bukan trading bot. Tidak ada wallet yang terhubung.
- State (daftar subscriber & timestamp terakhir) disimpan di `data/state.json`
  supaya tidak mengirim ulang notifikasi lama kalau bot direstart.
- Endpoint yang dipakai: `GET https://www.stonkfun.xyz/api/public/v1/tokens?sort=newest`
  — rate limit publiknya 300 request/menit per IP, jauh di atas kebutuhan
  polling 20 detik sekali.
- Kalau mau upgrade jadi auto-buy/sniping, StonkFun juga menyediakan endpoint
  `/launches/prepare` + `/launches/submit`, tapi itu butuh wallet Solana asli
  yang menandatangani transaksi — risiko finansial jauh lebih tinggi dan di
  luar cakupan bot notifikasi ini.
