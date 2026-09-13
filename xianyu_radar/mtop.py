"""mtop 协议客户端：签名、请求、登录态刷新。

mtop 是阿里系 H5 接口的通用协议。请求需要携带签名：

    sign = md5(f"{token}&{t}&{appKey}&{data}")

其中 token 来自 cookie `_m_h5_tk` 中第一个下划线之前的部分。
服务端可能在任意响应里刷新该 cookie，所以每次请求都从
cookie jar 读取最新 token；遇到 token 错误时自动重试一次。
"""

from __future__ import annotations

import hashlib
import json
import time
from typing import Any

import httpx

API_BASE = "https://h5api.m.goofish.com/h5"
APP_KEY = "34839810"
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36"
)

# 服务端在 token 失效时返回的错误码
_TOKEN_ERROR_CODES = {"FAIL_SYS_TOKEN_EXOIRED", "FAIL_SYS_TOKEN_EMPTY",
                      "FAIL_SYS_TOKEN_INVALID", "FAIL_SYS_ILLEGAL_ACCESS"}


class RadarError(Exception):
    """本项目的所有业务异常基类。"""


class SessionExpired(RadarError):
    """登录态失效，需要重新扫码。"""


class TokenError(RadarError):
    """mtop token 异常。"""


class RateLimited(RadarError):
    """被风控限流。"""


class ParseError(RadarError):
    """响应结构不符合预期。"""


def sign(token: str, ts: str, app_key: str, data: str) -> str:
    """计算 mtop 请求签名。"""
    raw = f"{token}&{ts}&{app_key}&{data}"
    return hashlib.md5(raw.encode("utf-8")).hexdigest()


class MtopClient:
    """针对 h5api.m.goofish.com 的轻量 HTTP 客户端。

    只做三件事：签名、发请求、解析出 data 字段。
    不含任何业务逻辑。
    """

    def __init__(self, cookies: list[dict[str, Any]], timeout: float = 20.0) -> None:
        jar = httpx.Cookies()
        for c in cookies:
            # 保留 domain/path，这样服务端刷新 _m_h5_tk 时能正确覆盖
            # 旧值，避免两个同名 cookie 并存导致 CookieConflict
            jar.set(
                c["name"],
                c["value"],
                domain=c.get("domain") or ".goofish.com",
                path=c.get("path") or "/",
            )
        self._client = httpx.Client(
            cookies=jar,
            timeout=timeout,
            headers={"User-Agent": USER_AGENT, "Referer": "https://www.goofish.com/"},
            follow_redirects=True,
        )

    # ---------- 内部工具 ----------

    def _token(self) -> str:
        """从 cookie 中取 mtop token。"""
        for c in self._client.cookies.jar:
            if c.name == "_m_h5_tk" and c.value:
                return c.value.split("_", 1)[0]
        raise SessionExpired("找不到 _m_h5_tk cookie，登录态可能已过期，请重新 login")

    def _has_login(self) -> bool:
        names = {c.name for c in self._client.cookies.jar}
        return "unb" in names or "cookie2" in names

    # ---------- 公开接口 ----------

    def call(self, api: str, data: dict[str, Any] | None = None,
             *, retry: int = 1) -> Any:
        """调用一个 mtop 接口，返回响应中的 data 字段。

        api 形如 "mtop.taobao.idlemtopsearch.pc.search"。
        """
        payload = dict(data or {})
        url = f"{API_BASE}/{api}/1.0/"
        last_err: Exception | None = None

        for attempt in range(retry + 1):
            ts = str(int(time.time() * 1000))
            body = _encode(payload)
            params = {
                "jsv": "2.7.2",
                "appKey": APP_KEY,
                "t": ts,
                "sign": sign(self._token(), ts, APP_KEY, body),
                "api": api,
                "v": "1.0",
                "type": "originaljson",
                "dataType": "json",
                "data": body,
            }
            try:
                resp = self._client.post(url, data=params)
            except httpx.HTTPError as e:
                last_err = RadarError(f"网络请求失败: {e}")
                time.sleep(1.0 + attempt)
                continue

            if resp.status_code == 429:
                raise RateLimited("被限流（HTTP 429），请降低频率后重试")

            try:
                payload_json = resp.json()
            except ValueError as e:
                last_err = ParseError(f"响应不是 JSON: {e}")
                continue

            ret = payload_json.get("ret") or []
            code = ""
            if ret and isinstance(ret[0], str):
                code = ret[0].split("::", 1)[0]

            if code in _TOKEN_ERROR_CODES and attempt < retry:
                # token 过期：服务端通常已在响应里刷新 cookie，重试即可
                time.sleep(0.3)
                continue

            if code and code != "SUCCESS":
                msg = ret[0] if ret else code
                if "TOKEN" in code:
                    raise SessionExpired(f"登录态失效: {msg}")
                if "FREQUENCY" in code or "LIMIT" in code:
                    raise RateLimited(f"触发限流: {msg}")
                raise RadarError(f"接口返回错误: {msg}")

            if "data" not in payload_json:
                raise ParseError(f"响应缺少 data 字段: {str(payload_json)[:200]}")
            return payload_json["data"]

        raise last_err or RadarError("请求失败且无更多信息")

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "MtopClient":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


def _encode(data: dict[str, Any]) -> str:
    """把参数编码成 mtop 要求的 JSON 字符串（紧凑、不转义中文）。"""
    return json.dumps(data, ensure_ascii=False, separators=(",", ":"))
