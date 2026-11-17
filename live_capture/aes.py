"""纯 Python 的 AES-128-CBC 解密，只为 HLS 的 ``EXT-X-KEY:METHOD=AES-128`` 服务。

不依赖 cryptography / pycryptodome，保证在一台干净的机器上也能直接跑。
S-box 由 GF(2^8) 乘法逆元 + 仿射变换现场推导，避免手抄常量表出错。
"""

from __future__ import annotations

from functools import lru_cache


def _xtime(a: int) -> int:
    a <<= 1
    return (a ^ 0x1B) & 0xFF if a & 0x100 else a


def _mul(a: int, b: int) -> int:
    """GF(2^8) 乘法（AES 多项式 0x11B）。"""
    result = 0
    for _ in range(8):
        if b & 1:
            result ^= a
        a = _xtime(a)
        b >>= 1
    return result


def _rotl8(x: int, shift: int) -> int:
    return ((x << shift) | (x >> (8 - shift))) & 0xFF


@lru_cache(maxsize=1)
def _tables() -> tuple[tuple[int, ...], tuple[int, ...]]:
    # GF(2^8) 逆元表
    inverse = [0] * 256
    for a in range(1, 256):
        for b in range(1, 256):
            if _mul(a, b) == 1:
                inverse[a] = b
                break
    sbox = [0] * 256
    for a in range(256):
        x = inverse[a]
        sbox[a] = (x ^ _rotl8(x, 1) ^ _rotl8(x, 2) ^ _rotl8(x, 3) ^ _rotl8(x, 4) ^ 0x63) & 0xFF
    inv_sbox = [0] * 256
    for i, s in enumerate(sbox):
        inv_sbox[s] = i
    # 已知向量自检，写错了宁可 import 时就炸
    assert sbox[0x00] == 0x63 and sbox[0x01] == 0x7C and sbox[0x53] == 0xED
    assert inv_sbox[0x63] == 0x00 and inv_sbox[0x7C] == 0x01
    return tuple(sbox), tuple(inv_sbox)


SBOX, INV_SBOX = _tables()


def _expand_key(key: bytes) -> list[list[int]]:
    if len(key) != 16:
        raise ValueError("仅支持 AES-128（16 字节密钥）")
    rcon = 1
    words = [list(key[i:i + 4]) for i in range(0, 16, 4)]
    for i in range(4, 44):
        temp = words[i - 1][:]
        if i % 4 == 0:
            temp = temp[1:] + temp[:1]  # RotWord
            temp = [SBOX[b] for b in temp]  # SubWord
            temp[0] ^= rcon
            rcon = _xtime(rcon)
        words.append([words[i - 4][j] ^ temp[j] for j in range(4)])
    return [sum(words[4 * r:4 * r + 4], []) for r in range(11)]


def _add_round_key(state: list[int], round_key: list[int]) -> None:
    for i in range(16):
        state[i] ^= round_key[i]


def _sub_bytes(state: list[int], box: tuple[int, ...]) -> None:
    for i in range(16):
        state[i] = box[state[i]]


def _shift_rows(state: list[int], inverse: bool = False) -> None:
    # state[r + 4c]，第 r 行循环左移 r（逆变换则右移 r）
    for row in range(1, 4):
        values = [state[row + 4 * c] for c in range(4)]
        shift = -row if inverse else row
        values = values[shift:] + values[:shift]
        for c in range(4):
            state[row + 4 * c] = values[c]


def _mix_columns(state: list[int], inverse: bool = False) -> None:
    matrix = (14, 11, 13, 9) if inverse else (2, 3, 1, 1)
    for c in range(4):
        col = state[4 * c:4 * c + 4]
        state[4 * c + 0] = _mul(col[0], matrix[0]) ^ _mul(col[1], matrix[1]) ^ _mul(col[2], matrix[2]) ^ _mul(col[3], matrix[3])
        state[4 * c + 1] = _mul(col[0], matrix[3]) ^ _mul(col[1], matrix[0]) ^ _mul(col[2], matrix[1]) ^ _mul(col[3], matrix[2])
        state[4 * c + 2] = _mul(col[0], matrix[2]) ^ _mul(col[1], matrix[3]) ^ _mul(col[2], matrix[0]) ^ _mul(col[3], matrix[1])
        state[4 * c + 3] = _mul(col[0], matrix[1]) ^ _mul(col[1], matrix[2]) ^ _mul(col[2], matrix[3]) ^ _mul(col[3], matrix[0])


def _decrypt_block(block: bytes, round_keys: list[list[int]]) -> bytes:
    state = list(block)
    _add_round_key(state, round_keys[10])
    for rnd in range(9, 0, -1):
        _shift_rows(state, inverse=True)
        _sub_bytes(state, INV_SBOX)
        _add_round_key(state, round_keys[rnd])
        _mix_columns(state, inverse=True)
    _shift_rows(state, inverse=True)
    _sub_bytes(state, INV_SBOX)
    _add_round_key(state, round_keys[0])
    return bytes(state)


def decrypt_cbc(ciphertext: bytes, key: bytes, iv: bytes) -> bytes:
    """AES-128-CBC 解密，并去掉 PKCS#7 填充。"""
    if len(key) != 16:
        raise ValueError("AES-128 需要 16 字节密钥")
    if len(iv) != 16:
        raise ValueError("需要 16 字节 IV")
    if len(ciphertext) % 16:
        raise ValueError(f"密文长度 {len(ciphertext)} 不是 16 的倍数")
    round_keys = _expand_key(key)
    plain = bytearray()
    prev = iv
    for offset in range(0, len(ciphertext), 16):
        chunk = ciphertext[offset:offset + 16]
        decrypted = _decrypt_block(chunk, round_keys)
        plain.extend(a ^ b for a, b in zip(decrypted, prev))
        prev = chunk
    return _strip_pkcs7(bytes(plain))


def _strip_pkcs7(data: bytes) -> bytes:
    if not data:
        return data
    pad = data[-1]
    if 1 <= pad <= 16 and len(data) >= pad and data[-pad:] == bytes([pad]) * pad:
        return data[:-pad]
    # 填充不合法时不强行裁剪，交由 ffmpeg 容错
    return data
