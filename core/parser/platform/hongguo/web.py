"""红果匿名播放接口的签名请求与响应校验。"""

import json
import secrets
import time
from typing import Any, Dict
from urllib.parse import urlencode

import aiohttp
from yarl import URL

from .sign import build_signature


_API_BASE = "https://api5-normal-sinfonlineb.fqnovel.com"
_MODEL_PATH = "/novel/player/video_model/v1/"
_MAX_RESPONSE_BYTES = 4 * 1024 * 1024
_USER_AGENT = (
    "com.phoenix.read/73532 (Linux; U; Android 16; zh_CN; 25053RT47C; "
    "Build/BP2A.250605.031.A3; Cronet/TTNetVersion:04657795 2026-01-23 "
    "QuicVersion:c67e9834 2025-09-08)"
)


class HongguoClient:
    """在解析器生命周期内复用匿名设备标识。"""

    def __init__(self) -> None:
        self._device_id = str(10**18 + secrets.randbelow(8 * 10**18))
        self._install_id = str(10**18 + secrets.randbelow(8 * 10**18))

    async def request(
        self,
        session: aiohttp.ClientSession,
        path: str,
        payload: Dict[str, Any],
    ) -> Dict[str, Any]:
        """请求官方播放接口，并校验有限长度的 JSON 响应。

        Args:
            session: 解析器共享的异步网络会话。
            path: 官方播放接口路径。
            payload: 播放请求参数。

        Returns:
            接口返回的数据对象。

        Raises:
            ValueError: 接口地址、状态或响应格式无效。
        """
        if path != _MODEL_PATH:
            raise ValueError("红果播放接口路径无效")
        now = time.time()
        params = {
            "aid": "8662",
            "app_name": "novelread",
            "version_code": "73532",
            "version_name": "7.3.5.32",
            "manifest_version_code": "73532",
            "update_version_code": "73532",
            "channel": "update_64",
            "device_platform": "android",
            "os": "android",
            "ssmix": "a",
            "device_type": "25053RT47C",
            "device_brand": "Redmi",
            "language": "zh",
            "os_api": "36",
            "os_version": "16",
            "resolution": "1280*2772",
            "dpi": "520",
            "ac": "wifi",
            "device_id": self._device_id,
            "iid": self._install_id,
            "_rticket": str(int(now * 1000)),
        }
        query = urlencode(sorted(params.items()))
        body = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
        headers = {
            "User-Agent": _USER_AGENT,
            "Accept": "application/json",
            "X-XS-From-Web": "0",
            "Sdk-Version": "2",
            "Content-Type": "application/json; charset=utf-8",
            **build_signature(query, body, now),
        }
        # 保留签名对应的 %2A 等编码，避免 aiohttp 再次编码导致验签失败。
        url = URL(f"{_API_BASE}{path}?{query}", encoded=True)
        async with session.post(
            url,
            data=body,
            headers=headers,
            timeout=aiohttp.ClientTimeout(total=30),
            allow_redirects=False,
        ) as response:
            if response.status != 200:
                raise ValueError(f"红果播放接口请求失败（HTTP {response.status}）")
            content = bytearray()
            async for chunk in response.content.iter_chunked(64 * 1024):
                content.extend(chunk)
                if len(content) > _MAX_RESPONSE_BYTES:
                    raise ValueError("红果播放接口响应过大")
        if not content:
            raise ValueError("红果播放接口未返回有效数据")
        try:
            result = json.loads(content)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise ValueError("红果播放接口响应格式异常") from exc
        if not isinstance(result, dict):
            raise ValueError("红果播放接口响应格式异常")
        codes = [result[key] for key in ("code", "Code", "status_code") if key in result]
        base_response = result.get("BaseResp")
        if isinstance(base_response, dict) and "StatusCode" in base_response:
            codes.append(base_response["StatusCode"])
        if any(code not in (0, "0") for code in codes):
            raise ValueError("红果播放接口返回失败状态")
        return result
