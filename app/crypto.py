"""QQ 数据库字段解密（SecurityUtile，逐码元 XOR）。"""

from __future__ import annotations


def xor_bytes(data: bytes, key: str) -> bytes:
    if not key:
        return data
    kb = key.encode("latin-1", errors="ignore")
    n = len(kb)
    return bytes(b ^ kb[i % n] for i, b in enumerate(data))


def decode(value, key: str) -> str:
    """把数据库里读出的 bytes/str 解成可读字符串。

    uin、群号等是 ASCII 字节逐个 XOR；昵称是 UTF-8 文本按码元 XOR，
    与 src/dataParsing/parsing.py 的 decrypt 行为保持一致。
    """
    if value is None:
        return ""
    if isinstance(value, (bytes, bytearray)):
        raw = xor_bytes(bytes(value), key)
        try:
            return raw.decode("utf-8")
        except UnicodeDecodeError:
            return raw.decode("latin-1")
    if isinstance(value, str):
        return "".join(chr(ord(ch) ^ ord(key[i % len(key)])) for i, ch in enumerate(value))
    return str(value)
