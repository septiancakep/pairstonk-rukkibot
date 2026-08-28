#!/usr/bin/env python3
"""Titik masuk sniper Raydium CPMM.

    python sniper_bot.py check          # uji konfigurasi & koneksi
    python sniper_bot.py prepare 1.0    # bungkus 1 SOL jadi WSOL
    python sniper_bot.py run            # jalankan sniper
"""

import sys

from sniper.cli import main

if __name__ == "__main__":
    sys.exit(main())
