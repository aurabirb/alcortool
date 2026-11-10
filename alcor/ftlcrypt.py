"""AlcorMP's FTL-table record codec (the FA 0B 21 housekeeping pages).

- Each 0x100-B table chunk is AES-256-CBC encrypted with a zero IV. The key is the 32-B
  string below with byte 0 = 0x66 and the chunk's index within its table XORed into
  bytes 10 and 20.
- The 0x100 B of ciphertext become two 0x100-B records: 0x80 B of ciphertext, 0xAA, a
  random byte (not checked), and 126 B of Reed-Solomon parity over the 0x81 B before the
  random byte (RS(255,129) over GF(256)/0x11d, first root alpha^1, symbols fed last to
  first).
- The table header uses the same key with byte 0 = 0x99 and byte 9 = 0x8f, encrypts the
  struct's bytes 0x10..0xff and lays the result out around a 16-B signature.

Decodes and re-encodes all 50 captured 21 pages byte-exact (given each record's random
byte). Where AlcorMP does it: re/alcormp-code.md (housekeeping tables).
"""
import os

KEY = b"D0o1n2'3t4T5e6l7l8A9nAyBbCoDdEyF"
SIGNATURE = bytes.fromhex("b3b3b9abbebdb3ba6986058f6986aa55")
CHUNK = 0x100                                   # plaintext per key / per record pair
RECORD = 0x100                                  # encoded record (0x80 B ciphertext + tail)


# ----------------------------------- AES -----------------------------------

def _xtime(a):
    return ((a << 1) ^ 0x1B) & 0xFF if a & 0x80 else a << 1


def _gmul(a, b):
    r = 0
    while b:
        if b & 1:
            r ^= a
        a, b = _xtime(a), b >> 1
    return r


def _sbox():
    s, inv = [0] * 256, [0] * 256
    for x in range(256):
        y = 0 if x == 0 else next(v for v in range(1, 256) if _gmul(x, v) == 1)
        b = y
        for k in range(1, 5):
            b ^= ((y << k) | (y >> (8 - k))) & 0xFF
        s[x] = b ^ 0x63
        inv[s[x]] = x
    return s, inv


SBOX, INV_SBOX = _sbox()
_MUL = {k: [_gmul(x, k) for x in range(256)] for k in (2, 3, 9, 11, 13, 14)}


def _expand(key):
    nk = len(key) // 4
    w = [list(key[4 * i:4 * i + 4]) for i in range(nk)]
    rcon = 1
    for i in range(nk, 4 * (nk + 7)):
        t = list(w[-1])
        if i % nk == 0:
            t = [SBOX[b] for b in t[1:] + t[:1]]
            t[0] ^= rcon
            rcon = _xtime(rcon)
        elif nk > 6 and i % nk == 4:
            t = [SBOX[b] for b in t]
        w.append([a ^ b for a, b in zip(w[i - nk], t)])
    return [sum(w[4 * r:4 * r + 4], []) for r in range(nk + 7)]


def _enc_block(rk, b):
    m2, m3 = _MUL[2], _MUL[3]
    s = [x ^ k for x, k in zip(b, rk[0])]
    for r in range(1, len(rk)):
        s = [SBOX[s[(i + 4 * (i % 4)) % 16]] for i in range(16)]
        if r != len(rk) - 1:
            s = [v for c in range(4) for v in (
                m2[s[4 * c]] ^ m3[s[4 * c + 1]] ^ s[4 * c + 2] ^ s[4 * c + 3],
                s[4 * c] ^ m2[s[4 * c + 1]] ^ m3[s[4 * c + 2]] ^ s[4 * c + 3],
                s[4 * c] ^ s[4 * c + 1] ^ m2[s[4 * c + 2]] ^ m3[s[4 * c + 3]],
                m3[s[4 * c]] ^ s[4 * c + 1] ^ s[4 * c + 2] ^ m2[s[4 * c + 3]])]
        s = [x ^ k for x, k in zip(s, rk[r])]
    return bytes(s)


def _dec_block(rk, b):
    m9, m11, m13, m14 = _MUL[9], _MUL[11], _MUL[13], _MUL[14]
    s = [x ^ k for x, k in zip(b, rk[-1])]
    for r in range(len(rk) - 2, -1, -1):
        s = [INV_SBOX[s[(i - 4 * (i % 4)) % 16]] for i in range(16)]
        s = [x ^ k for x, k in zip(s, rk[r])]
        if r:
            s = [v for c in range(4) for v in (
                m14[s[4 * c]] ^ m11[s[4 * c + 1]] ^ m13[s[4 * c + 2]] ^ m9[s[4 * c + 3]],
                m9[s[4 * c]] ^ m14[s[4 * c + 1]] ^ m11[s[4 * c + 2]] ^ m13[s[4 * c + 3]],
                m13[s[4 * c]] ^ m9[s[4 * c + 1]] ^ m14[s[4 * c + 2]] ^ m11[s[4 * c + 3]],
                m11[s[4 * c]] ^ m13[s[4 * c + 1]] ^ m9[s[4 * c + 2]] ^ m14[s[4 * c + 3]])]
    return bytes(s)


def cbc_encrypt(key, data, iv=bytes(16)):
    rk, out, c = _expand(key), bytearray(), iv
    for i in range(0, len(data), 16):
        c = _enc_block(rk, bytes(a ^ b for a, b in zip(data[i:i + 16], c)))
        out += c
    return bytes(out)


def cbc_decrypt(key, data, iv=bytes(16)):
    rk, out, c = _expand(key), bytearray(), iv
    for i in range(0, len(data), 16):
        blk = data[i:i + 16]
        out += bytes(a ^ b for a, b in zip(_dec_block(rk, blk), c))
        c = blk
    return bytes(out)


def table_key(index):
    """the key of a table's index-th 0x100-B chunk."""
    k = bytearray(KEY)
    k[0] = 0x66
    k[10] ^= index & 0xFF
    k[20] ^= index & 0xFF
    return bytes(k)


def header_key():
    """the header's key."""
    k = bytearray(KEY)
    k[0], k[9] = 0x99, 0x8F
    return bytes(k)


# ------------------------------- Reed-Solomon -------------------------------

def _gf():
    exp, log = [0] * 255, [-1] * 256
    x = 1
    for i in range(255):
        exp[i], log[x] = x, i
        x <<= 1
        if x & 0x100:
            x ^= 0x11D
    g = [1]                                     # prod (x - alpha^i), i = 1..126
    for i in range(1, 127):
        r, ng = exp[i], [0] * (len(g) + 1)
        for j, c in enumerate(g):
            ng[j] ^= 0 if c == 0 else exp[(log[c] + log[r]) % 255]
            ng[j + 1] ^= c
        g = ng
    return exp, log, [log[c] for c in g]        # generator in log form, g[0] first


_EXP, _LOG, _GEN = _gf()
NPAR = 0x7E


def rs_parity(data):
    """126 parity bytes over data[0..0x80], fed from 0x80 down to 0."""
    p = [0] * NPAR
    for i in range(0x80, -1, -1):
        fb = _LOG[data[i] ^ p[NPAR - 1]]
        if fb == -1:
            p = [0] + p[:-1]
            continue
        p = [_EXP[(_GEN[0] + fb) % 255]] + [
            (_EXP[(_GEN[j] + fb) % 255] if _GEN[j] != -1 else 0) ^ p[j - 1]
            for j in range(1, NPAR)]
    return bytes(p)


# --------------------------------- records ---------------------------------

def _record(ct, rand):
    rec = bytes(ct) + b"\xaa" + bytes([rand])
    return rec + rs_parity(rec)


def _rand():
    return os.urandom(1)[0]                     # AlcorMP: rand() after srand(time)


def encode_chunk(plain, index, rand=_rand):
    """One 0x100-B table chunk -> 0x200 B (two records)."""
    ct = cbc_encrypt(table_key(index), plain)
    return _record(ct[:0x80], rand()) + _record(ct[0x80:], rand())


def encode_header(struct_, rand=_rand):
    """The 0x100-B table header -> 0x200 B: bytes 0..7 and 0x20..0x27
    carry the signature; the cipher runs over bytes 0x10..0xff with 0x20..0x27 taken
    from 0x08..0x0f."""
    p = bytearray(struct_)
    p[0x20:0x28] = p[0x08:0x10]
    ct = cbc_encrypt(header_key(), bytes(p[0x10:0x100]))
    out = SIGNATURE[0:8] + ct[0x10:0x18] + ct[0:0x10] + SIGNATURE[8:16] + ct[0x18:0xF0]
    return _record(out[:0x80], rand()) + _record(out[0x80:], rand())


def decode_chunk(enc, index):
    """Inverse of encode_chunk (parity not checked)."""
    return cbc_decrypt(table_key(index), enc[0:0x80] + enc[0x100:0x180])


def decode_header(enc):
    """The header struct's bytes 0x10..0xff (bytes 0x20..0x27 = struct 0x08..0x0f)."""
    buf = enc[0:0x80] + enc[0x100:0x180]
    return cbc_decrypt(header_key(), buf[0x10:0x20] + buf[0x08:0x10] + buf[0x28:0x100])


def self_test():
    k = bytes(range(32))                        # FIPS-197 C.3
    assert _enc_block(_expand(k), bytes.fromhex("00112233445566778899aabbccddeeff")).hex() \
        == "8ea2b7ca516745bfeafc49904b496089"
    plain = bytes(range(256))
    for i in (0, 5, 63):
        assert decode_chunk(encode_chunk(plain, i), i) == plain
    hdr = bytes(0xF0) + bytes.fromhex("ffffffffffffffffffff07ea0a190000")
    assert decode_header(encode_header(hdr))[0xE0:] == hdr[0xF0:]
    rec = encode_chunk(plain, 0)[:RECORD]
    assert rec[0x80] == 0xAA and rs_parity(rec) == rec[0x82:]
    return True


if __name__ == "__main__":
    print("ftlcrypt self-test:", "PASS" if self_test() else "FAIL")
