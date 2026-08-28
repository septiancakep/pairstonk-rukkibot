"""Base58 (alfabet Bitcoin) — cukup untuk data instruksi yang pendek."""

from __future__ import annotations

_ALPHABET = b"123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
_INDEX = {c: i for i, c in enumerate(_ALPHABET)}


def b58decode(value: str | bytes) -> bytes:
    raw = value.encode() if isinstance(value, str) else value
    number = 0
    for char in raw:
        digit = _INDEX.get(char)
        if digit is None:
            raise ValueError(f"karakter base58 tidak valid: {chr(char)!r}")
        number = number * 58 + digit
    leading_zeros = len(raw) - len(raw.lstrip(b"1"))
    body = number.to_bytes((number.bit_length() + 7) // 8, "big") if number else b""
    return b"\x00" * leading_zeros + body


def b58encode(data: bytes) -> str:
    number = int.from_bytes(data, "big")
    out = bytearray()
    while number:
        number, rem = divmod(number, 58)
        out.append(_ALPHABET[rem])
    out.extend(b"1" * (len(data) - len(data.lstrip(b"\x00"))))
    return out[::-1].decode()
