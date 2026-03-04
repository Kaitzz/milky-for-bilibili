"""
B站认证模块 —— Cookie 管理与请求 Headers 构造
"""

from __future__ import annotations

import httpx


class BiliAuth:
    """封装 B站登录态，提供可复用的 httpx.AsyncClient。"""

    # 通用请求头，模拟浏览器访问
    DEFAULT_HEADERS = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/125.0.0.0 Safari/537.36"
        ),
        "Referer": "https://www.bilibili.com/",
        "Origin": "https://www.bilibili.com",
    }

    def __init__(self, sessdata: str, bili_jct: str, dedeuserid: str):
        self.sessdata = sessdata
        self.bili_jct = bili_jct          # 即 csrf token
        self.dedeuserid = dedeuserid

    @property
    def cookies(self) -> dict[str, str]:
        return {
            "SESSDATA": self.sessdata,
            "bili_jct": self.bili_jct,
            "DedeUserID": self.dedeuserid,
        }

    @property
    def cookie_header(self) -> str:
        """生成 Cookie 请求头字符串，避免被 Set-Cookie 覆盖。"""
        parts = [f"{k}={v}" for k, v in self.cookies.items()]
        return "; ".join(parts)

    def build_client(self, **kwargs) -> httpx.AsyncClient:
        """创建带认证信息的 httpx 异步客户端。"""
        headers = {**self.DEFAULT_HEADERS, "Cookie": self.cookie_header}
        return httpx.AsyncClient(
            headers=headers,
            timeout=httpx.Timeout(30.0),
            follow_redirects=True,
            **kwargs,
        )
