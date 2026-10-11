"""红果匿名播放请求签名与视频内容密钥解码。"""

import base64
import binascii
import hashlib
from typing import Dict


def build_signature(query: str, body: bytes, now: float) -> Dict[str, str]:
    """根据实际传输的查询串和请求体生成签名头。

    Args:
        query: 已编码且不会再次改写的查询串。
        body: 实际发送的请求体字节。
        now: 当前 Unix 时间，单位为秒。

    Returns:
        播放接口所需的签名请求头。
    """
    timestamp = int(now) & 0xFFFFFFFF
    query_digest = hashlib.md5(query.encode("utf-8")).digest()
    body_digest = hashlib.md5(body).digest()
    payload = bytearray(
        query_digest[:4]
        + body_digest[:4]
        + bytes(4)
        + bytes((0, 6, 11, 28))
        + timestamp.to_bytes(4, "big")
    )
    mask = bytes.fromhex("44b9b9d9a4aef9fca493aa757ca3c2c4a496938f")
    for index, value in enumerate(mask):
        payload[index] ^= value
    # 混合过程依赖已经写回的首字节，不能改为同时计算全部字节。
    for index, value in enumerate(payload):
        rotated = ((value << 4) | (value >> 4)) & 0xFF
        mixed = rotated ^ payload[(index + 1) % len(payload)]
        reversed_bits = int(f"{mixed:08b}"[::-1], 2)
        payload[index] = reversed_bits ^ 0xFF ^ len(payload)
    return {
        "X-Khronos": str(timestamp),
        "X-Gorgon": (bytes.fromhex("8404401c0000") + payload).hex(),
        "X-SS-Req-Ticket": str(int(now * 1000)),
        "X-SS-STUB": body_digest.hex().upper(),
    }


def decode_video_key(spade: str) -> str:
    """解码播放接口提供的 AES-128 内容密钥。

    Args:
        spade: 播放源中的 spade_a 字段。

    Returns:
        32 位十六进制内容密钥。

    Raises:
        ValueError: 密钥编码、结构或版本无效。
    """
    if not isinstance(spade, str) or len(spade) > 1024:
        raise ValueError("红果媒体密钥数据无效")
    value = spade.strip()
    try:
        raw = base64.b64decode(value + "=" * (-len(value) % 4), validate=True)
    except (ValueError, binascii.Error) as exc:
        raise ValueError("红果媒体密钥编码无效") from exc
    if len(raw) < 3:
        raise ValueError("红果媒体密钥结构无效")
    tag_length = (raw[0] ^ raw[1] ^ raw[2]) - 48
    content_length = len(raw) - tag_length - 1
    if tag_length < 1 or content_length < 33 or content_length >= len(raw):
        raise ValueError("红果媒体密钥结构无效")
    seed = raw[-tag_length - 2] ^ raw[-tag_length - 1]
    tag = bytes(current ^ seed for current in raw[-tag_length:])
    if tag in (b"app_v2", b"web_v2"):
        raise ValueError("红果媒体密钥版本暂不支持")
    decoded = bytearray()
    previous = [250, 85]
    for index, current in enumerate(raw[1 : 1 + content_length]):
        slot = index % 2
        decoded.append(((previous[slot] ^ current) - 21 - index.bit_count()) & 0xFF)
        previous[slot] = current
    try:
        padding = int(chr(decoded[0]), 36)
        key = bytes.fromhex(decoded[1:33].decode("ascii"))
    except (ValueError, UnicodeDecodeError) as exc:
        raise ValueError("红果媒体密钥内容无效") from exc
    if content_length - padding - 1 != 32 or len(key) != 16:
        raise ValueError("红果媒体密钥内容无效")
    return key.hex()
