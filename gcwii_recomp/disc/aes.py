"""AES-128-CBC decryption used for Wii partitions.

Uses the ``cryptography`` package when it is installed (fast); otherwise
falls back to a small pure-Python implementation so the tool has no hard
third-party dependencies.  The fallback is slow (~1 MB/s) but is fine for
extracting a DOL and FST.
"""
from __future__ import annotations

try:  # pragma: no cover - depends on environment
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

    def aes_cbc_decrypt(key: bytes, iv: bytes, data: bytes) -> bytes:
        dec = Cipher(algorithms.AES(key), modes.CBC(iv)).decryptor()
        return dec.update(data) + dec.finalize()

    def aes_cbc_encrypt(key: bytes, iv: bytes, data: bytes) -> bytes:
        enc = Cipher(algorithms.AES(key), modes.CBC(iv)).encryptor()
        return enc.update(data) + enc.finalize()

    BACKEND = "cryptography"
except ImportError:  # pragma: no cover
    BACKEND = "pure-python"

    def aes_cbc_decrypt(key: bytes, iv: bytes, data: bytes) -> bytes:
        return _PyAES(key).cbc_decrypt(iv, data)

    def aes_cbc_encrypt(key: bytes, iv: bytes, data: bytes) -> bytes:
        return _PyAES(key).cbc_encrypt(iv, data)


def _build_tables():
    sbox = [0] * 256
    inv = [0] * 256
    p = q = 1
    while True:
        p = p ^ ((p << 1) & 0xFF) ^ (0x1B if p & 0x80 else 0)
        q ^= q << 1
        q ^= q << 2
        q ^= q << 4
        q &= 0xFF
        if q & 0x80:
            q ^= 0x09
        x = q ^ ((q << 1) | (q >> 7)) ^ ((q << 2) | (q >> 6)) ^ ((q << 3) | (q >> 5)) ^ ((q << 4) | (q >> 4))
        x = (x ^ 0x63) & 0xFF
        sbox[p] = x
        inv[x] = p
        if p == 1:
            break
    sbox[0] = 0x63
    inv[0x63] = 0
    return sbox, inv


_SBOX, _INV_SBOX = _build_tables()


def _xt(a: int) -> int:
    return ((a << 1) ^ 0x1B) & 0xFF if a & 0x80 else a << 1


def _mul(a: int, b: int) -> int:
    r = 0
    while b:
        if b & 1:
            r ^= a
        a = _xt(a)
        b >>= 1
    return r


class _PyAES:
    def __init__(self, key: bytes):
        if len(key) != 16:
            raise ValueError("AES-128 key must be 16 bytes")
        w = [list(key[i:i + 4]) for i in range(0, 16, 4)]
        rcon = 1
        for i in range(4, 44):
            t = list(w[i - 1])
            if i % 4 == 0:
                t = t[1:] + t[:1]
                t = [_SBOX[b] for b in t]
                t[0] ^= rcon
                rcon = _xt(rcon)
            w.append([w[i - 4][j] ^ t[j] for j in range(4)])
        self.rk = [sum(w[r * 4:r * 4 + 4], []) for r in range(11)]

    def _encrypt_block(self, b):
        s = [b[i] ^ self.rk[0][i] for i in range(16)]
        for rnd in range(1, 11):
            s = [_SBOX[x] for x in s]
            s = [s[(i + 4 * (i % 4)) % 16] for i in range(16)]
            if rnd != 10:
                n = []
                for c in range(4):
                    a = s[c * 4:c * 4 + 4]
                    n += [_mul(a[0], 2) ^ _mul(a[1], 3) ^ a[2] ^ a[3],
                          a[0] ^ _mul(a[1], 2) ^ _mul(a[2], 3) ^ a[3],
                          a[0] ^ a[1] ^ _mul(a[2], 2) ^ _mul(a[3], 3),
                          _mul(a[0], 3) ^ a[1] ^ a[2] ^ _mul(a[3], 2)]
                s = n
            s = [s[i] ^ self.rk[rnd][i] for i in range(16)]
        return s

    def _decrypt_block(self, b):
        s = [b[i] ^ self.rk[10][i] for i in range(16)]
        for rnd in range(9, -1, -1):
            s = [s[(i - 4 * (i % 4)) % 16] for i in range(16)]
            s = [_INV_SBOX[x] for x in s]
            s = [s[i] ^ self.rk[rnd][i] for i in range(16)]
            if rnd != 0:
                n = []
                for c in range(4):
                    a = s[c * 4:c * 4 + 4]
                    n += [_mul(a[0], 14) ^ _mul(a[1], 11) ^ _mul(a[2], 13) ^ _mul(a[3], 9),
                          _mul(a[0], 9) ^ _mul(a[1], 14) ^ _mul(a[2], 11) ^ _mul(a[3], 13),
                          _mul(a[0], 13) ^ _mul(a[1], 9) ^ _mul(a[2], 14) ^ _mul(a[3], 11),
                          _mul(a[0], 11) ^ _mul(a[1], 13) ^ _mul(a[2], 9) ^ _mul(a[3], 14)]
                s = n
        return s

    def cbc_decrypt(self, iv: bytes, data: bytes) -> bytes:
        out = bytearray()
        prev = list(iv)
        for i in range(0, len(data), 16):
            blk = list(data[i:i + 16])
            d = self._decrypt_block(blk)
            out += bytes(d[j] ^ prev[j] for j in range(16))
            prev = blk
        return bytes(out)

    def cbc_encrypt(self, iv: bytes, data: bytes) -> bytes:
        out = bytearray()
        prev = list(iv)
        for i in range(0, len(data), 16):
            blk = [data[i + j] ^ prev[j] for j in range(16)]
            prev = self._encrypt_block(blk)
            out += bytes(prev)
        return bytes(out)
