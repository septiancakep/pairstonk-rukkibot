# Sniper Raydium CPMM

Bot yang memantau program Raydium CP-Swap (CPMM) di Solana, mendeteksi pool
baru pada saat instruksi `initialize`-nya masuk, lalu langsung mengirim
transaksi beli — dilanjutkan take profit / stop loss otomatis.

> **Ini bot trading yang memegang private key dan membelanjakan dana sungguhan.**
> `DRY_RUN=true` adalah default. Jalankan minimal beberapa jam dalam mode itu
> dulu, baca lognya, baru matikan. Sniping token baru punya tingkat kerugian
> total yang tinggi: rug pull, LP ditarik, dev dump. Pakai wallet terpisah yang
> isinya siap kamu hilangkan.

---

## Bagaimana ini bisa cepat

Yang menentukan menang atau kalah bukan bahasa pemrogramannya, tapi berapa
banyak perjalanan jaringan antara "pool muncul" dan "transaksi terkirim".
Rancangan di sini menekan jumlahnya ke **nol**:

| Langkah | Cara umum | Di sini |
|---|---|---|
| Deteksi pool | `logsSubscribe` → `getTransaction` | `transactionSubscribe` — transaksi lengkap ikut di notifikasi |
| Cari alamat vault/pool | `getAccountInfo` pool state | Diurai dari 20 akun instruksi `initialize` |
| Cadangan awal | Baca saldo kedua vault | `init_amount_0` / `init_amount_1` di data instruksi |
| Blockhash | `getLatestBlockhash` saat butuh | Cache latar belakang, disegarkan tiap 500 ms |
| Akun WSOL | Buat + wrap + sync tiap transaksi | Dibungkus sekali di muka lewat `prepare` |
| Kirim | Satu RPC | Semua RPC paralel + bundle Jito opsional |

Sisa transaksi belinya cuma empat instruksi: dua compute budget, satu
`createIdempotent` untuk ATA token baru, dan `swap_base_input`. Muat dalam satu
paket UDP (tes memastikan < 1232 byte).

Latensi yang tersisa didominasi jarak ke node Geyser dan ke leader — bukan
Python. Perakitan transaksi memakai `solders` (Rust), ordenya di bawah satu
milidetik. Yang benar-benar memberi keunggulan: RPC yang bagus, priority fee
yang masuk akal, dan mesin yang dekat secara geografis dengan node itu.

### Dua mode deteksi

* **`DETECTOR=tx`** — `transactionSubscribe`, WebSocket berbasis Geyser
  (Helius, Triton, QuickNode). Transaksi lengkap sampai pada commitment
  `processed`, jadi tidak ada RPC susulan sama sekali. **Pakai ini.**
* **`DETECTOR=logs`** — `logsSubscribe`, jalan di RPC mana pun termasuk yang
  gratis. Tapi notifikasinya cuma berisi signature, dan `getTransaction` baru
  menjawab pada commitment `confirmed` — tambahan 300–800 ms. Untuk uji coba
  dan belajar, bukan untuk berlomba.

RPC publik `api.mainnet-beta.solana.com` tidak mendukung `transactionSubscribe`
dan rate limit-nya jauh terlalu ketat. Sniping tanpa RPC berbayar tidak realistis.

---

## Pasang

```bash
git clone <repo-ini> && cd pairstonk-rukkibot
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements-sniper.txt

cp .env.sniper.example .env
# edit .env: isi PRIVATE_KEY, RPC_HTTP_URLS, RPC_WS_URL
```

## Pakai

```bash
python sniper_bot.py check        # uji konfigurasi + koneksi RPC/WebSocket
python sniper_bot.py balance      # saldo SOL & WSOL, sisa jatah snipe
python sniper_bot.py prepare 1.0  # bungkus 1 SOL jadi WSOL (modal snipe)
python sniper_bot.py run          # jalankan sniper
python sniper_bot.py unwrap       # tutup WSOL ATA, kembalikan jadi SOL
```

`prepare` bukan sekadar kenyamanan: WSOL yang sudah dibungkus di muka memangkas
tiga instruksi dari setiap transaksi beli.

Selain WSOL, siapkan **SOL asli** secukupnya. Setiap pembelian membuat satu
token account baru, dan rent-nya (0.00204 SOL) keluar dari saldo SOL, bukan
dari WSOL.

---

## Konfigurasi

Semua lewat `.env`; daftar lengkap dengan penjelasan ada di
`.env.sniper.example`. Yang paling menentukan hasil:

| Variabel | Kenapa penting |
|---|---|
| `DETECTOR` | `tx` vs `logs` — selisihnya ratusan milidetik |
| `COMPUTE_UNIT_PRICE` | Penentu utama urutan masuk blok. Terlalu rendah = tidak pernah menang; terlalu tinggi = boros di transaksi yang gagal |
| `BUY_SOL_AMOUNT` | Sniper menolak beli lebih dari setengah isi kolam — order besar hanya membeli harganya sendiri |
| `SLIPPAGE_BPS` | Batas bawah jumlah token yang kamu terima. `0` berarti minta harga persis dan hampir selalu gagal; terlalu longgar berarti pasrah disandwich |
| `MIN_QUOTE_LIQUIDITY_SOL` | Kolam tipis paling gampang di-rug |
| `CHECK_MINT_AUTHORITY` | Menolak token yang masih bisa dicetak/dibekukan. Biayanya satu RPC (~30–80 ms) |
| `MAX_TOTAL_SPEND_SOL` | Batas keras total belanja satu sesi |

### Take profit bertingkat

`TAKE_PROFIT_LADDER=100:50,300:100` berarti: di +100% jual 50% posisi, lalu di
+300% jual seluruh sisanya. Anak tangga dieksekusi berurutan, dan porsinya
dihitung dari sisa yang masih dipegang.

`STOP_LOSS_PCT`, `TRAILING_STOP_PCT`, dan `MAX_HOLD_SECONDS` menjual seluruh
posisi begitu terpicu.

---

## Apa yang disaring dan apa yang tidak

Yang **ditolak** sebelum beli:

- Pool tanpa quote mint yang diizinkan (default: hanya SOL)
- Likuiditas awal di luar rentang min/max
- `BUY_SOL_AMOUNT` lebih dari setengah isi kolam
- Token memakai program Token-2022 (bisa punya transfer fee atau transfer hook
  yang membuat penjualan gagal) — kecuali `ALLOW_TOKEN_2022=true`
- Mint yang masih punya mint authority atau freeze authority
  (kalau `CHECK_MINT_AUTHORITY=true`)
- `open_time` yang masih jauh di depan
- Mint atau pembuat yang masuk daftar hitam

Yang **tidak** disaring, dan tetap jadi risikomu:

- **LP tidak dicek terkunci atau dibakar.** Pembuat pool bisa menarik seluruh
  likuiditas kapan saja. Ini penyebab rug paling umum dan tidak bisa diketahui
  pada saat `initialize` — informasinya belum ada.
- **Distribusi suplai tidak dicek.** Dev bisa memegang mayoritas token dan
  menjualnya ke arahmu.
- Lolos filter bukan berarti token itu aman. Filter hanya membuang yang paling
  jelas buruk.

---

## Deploy

Taruh di VPS yang dekat dengan node RPC-mu — latensi jaringan adalah keunggulan
terbesar yang bisa dibeli. Kalau memakai Helius, itu berarti region
Amerika Serikat.

```ini
# /etc/systemd/system/cpmm-sniper.service
[Unit]
Description=Raydium CPMM Sniper
After=network-online.target

[Service]
WorkingDirectory=/home/user/pairstonk-rukkibot
EnvironmentFile=/home/user/pairstonk-rukkibot/.env
ExecStart=/home/user/pairstonk-rukkibot/.venv/bin/python sniper_bot.py run
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload && sudo systemctl enable --now cpmm-sniper
journalctl -u cpmm-sniper -f
```

Jangan pakai platform PaaS berbagi (Railway/Render free tier) untuk ini —
latensi dan CPU-nya tidak bisa diandalkan.

---

## Struktur kode

```
sniper_bot.py         titik masuk CLI
sniper/
  constants.py        program ID, PDA otoritas, discriminator Anchor
  config.py           pemuatan .env, parsing keypair & tangga TP
  codec.py            layout PoolState/AmmConfig/mint, matematika constant product
  ixs.py              pembangun instruksi ATA, SPL Token, dan swap CP-Swap
  b58.py              base58 (data instruksi datang dalam bentuk ini)
  rpc.py              klien JSON-RPC async, siaran multi-endpoint
  blockhash.py        cache blockhash latar belakang
  detect.py           langganan WebSocket + ekstraksi pool dari transaksi
  filters.py          penyaringan pra-beli
  trader.py           pembangunan & pengiriman transaksi beli/jual
  positions.py        take profit bertingkat, stop loss, trailing, batas waktu
  sender.py           siaran RPC paralel + bundle Jito
  main.py             orkestrator dan pembatas risiko
tests/test_sniper.py  32 tes offline
```

## Tes

```bash
.venv/bin/python -m unittest discover -s tests -v
```

Seluruh jalur diuji tanpa jaringan: notifikasi WebSocket sintetis → ekstraksi
pool (termasuk kasus CPI dan address lookup table) → filter → instruksi swap →
transaksi bertandatangan. Discriminator Anchor dan PDA otoritas dihitung saat
runtime lalu dicocokkan dengan nilai Raydium yang diketahui, jadi salah ketik
konstanta akan ketahuan di tes, bukan di mainnet.

Yang **tidak** tercakup: eksekusi on-chain sungguhan. Sebelum memakai dana
besar, jalankan `DRY_RUN=true`, lalu coba dengan `BUY_SOL_AMOUNT` sekecil
mungkin.
