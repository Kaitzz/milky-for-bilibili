"""
B站 API 封装 —— 视频信息 / 字幕 / 评论 / @提及 消息
"""

from __future__ import annotations

import json
import logging
import re
import xml.etree.ElementTree as ET
from collections import Counter
from dataclasses import dataclass, field
from typing import Any

import httpx

from .auth import BiliAuth

logger = logging.getLogger(__name__)

# ── 数据模型 ──────────────────────────────────────────────


@dataclass
class VideoInfo:
    aid: int
    bvid: str
    title: str
    desc: str
    owner_name: str
    duration: int  # 秒
    subtitle_urls: list[dict[str, str]] = field(default_factory=list)


@dataclass
class MentionItem:
    """一条 @提及 消息。"""

    id: int  # 消息 id
    user_name: str  # 谁 @ 了我
    user_mid: int
    item_type: str  # "reply" / "at" 等
    source_id: int  # 该评论自身的 rpid（根评论 ID）
    root_id: int  # 根评论 rpid（0 表示 source_id 就是根评论）
    target_id: int  # 目标评论 rpid
    subject_id: int  # 视频的 aid
    uri: str  # 跳转链接，可提取 BV 号
    native_uri: str
    title: str  # 视频标题
    source_content: str  # 用户评论原文
    at_time: int  # 时间戳


# ── API Client ────────────────────────────────────────────


class BiliAPI:
    """B站 Web API 非官方封装（仅使用公开可访问的接口）。"""

    BASE = "https://api.bilibili.com"

    def __init__(self, auth: BiliAuth):
        self.auth = auth
        self._client: httpx.AsyncClient | None = None

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = self.auth.build_client()
        return self._client

    async def close(self):
        if self._client and not self._client.is_closed:
            await self._client.aclose()

    # ── 辅助 ──

    @staticmethod
    def bv_to_aid(bvid: str) -> int | None:
        """BV -> aid 的简易转换（不保证长期有效，可降级为 API 查询）。"""
        # 通过 API 在线查询更可靠，这里做 fallback
        return None

    @staticmethod
    def extract_bvid(text: str) -> str | None:
        """从文本/URL 中提取 BV 号。"""
        m = re.search(r"(BV[0-9A-Za-z]{10})", text)
        return m.group(1) if m else None

    @staticmethod
    def extract_opus_id(text: str) -> str | None:
        """从 URL 中提取动态(opus) ID。"""
        m = re.search(r"bilibili\.com/opus/(\d+)", text)
        return m.group(1) if m else None

    async def get_bvid_from_opus(self, opus_id: str) -> str | None:
        """通过动态 ID 查询关联的视频 BV 号。"""
        client = await self._get_client()
        try:
            resp = await client.get(
                f"{self.BASE}/x/polymer/web-dynamic/v1/detail",
                params={"id": opus_id},
            )
            data = resp.json()
            if data.get("code") != 0:
                logger.debug("获取动态详情失败: %s", data.get("message"))
                return None
            item = data.get("data", {}).get("item", {})
            # 视频动态：major.archive 中有 bvid
            major = item.get("modules", {}).get("module_dynamic", {}).get("major", {})
            if major and major.get("type") == "MAJOR_TYPE_ARCHIVE":
                bvid = major.get("archive", {}).get("bvid", "")
                if bvid:
                    return bvid
            # 备选：从 basic 的 jump_url 提取
            basic = item.get("basic", {})
            jump_url = basic.get("jump_url", "")
            bvid = self.extract_bvid(jump_url)
            if bvid:
                return bvid
            logger.debug("动态 %s 不包含视频", opus_id)
            return None
        except Exception:
            logger.exception("get_bvid_from_opus 异常")
            return None

    # ── 视频信息 ──

    async def get_video_info(self, bvid: str) -> VideoInfo | None:
        """获取视频基本信息（含字幕列表）。"""
        client = await self._get_client()
        try:
            resp = await client.get(
                f"{self.BASE}/x/web-interface/view",
                params={"bvid": bvid},
            )
            data = resp.json()
            if data.get("code") != 0:
                logger.warning("获取视频信息失败: %s", data.get("message"))
                return None
            v = data["data"]
            subtitle_list = (
                v.get("subtitle", {}).get("list", [])
            )
            subtitle_urls = [
                {"lang": s.get("lan_doc", ""), "url": s.get("subtitle_url", "")}
                for s in subtitle_list
            ]
            return VideoInfo(
                aid=v["aid"],
                bvid=v["bvid"],
                title=v["title"],
                desc=v.get("desc", ""),
                owner_name=v.get("owner", {}).get("name", ""),
                duration=v.get("duration", 0),
                subtitle_urls=subtitle_urls,
            )
        except Exception:
            logger.exception("get_video_info 异常")
            return None

    # ── 字幕 ──

    async def get_subtitle_text(self, subtitle_url: str) -> str:
        """下载并拼接字幕 JSON 为纯文本。"""
        client = await self._get_client()
        if subtitle_url.startswith("//"):
            subtitle_url = "https:" + subtitle_url
        try:
            resp = await client.get(subtitle_url)
            body = resp.json().get("body", [])
            lines = [item.get("content", "") for item in body]
            return "\n".join(lines)
        except Exception:
            logger.exception("get_subtitle_text 异常")
            return ""

    # ── 弹幕 ──

    async def get_danmaku(self, cid: int, max_count: int = 800) -> list[str]:
        """获取视频弹幕（XML 接口，公开无需登录）。

        返回去重后的弹幕文本列表（按出现频率降序）。
        """
        if cid == 0:
            return []
        url = f"https://comment.bilibili.com/{cid}.xml"
        client = await self._get_client()
        try:
            resp = await client.get(url)
            resp.raise_for_status()
            root = ET.fromstring(resp.content)
            raw = [d.text.strip() for d in root.findall("d") if d.text and d.text.strip()]
            if not raw:
                return []
            # 去重并按频率降序
            counter = Counter(raw)
            unique = [text for text, _ in counter.most_common()]
            return unique[:max_count]
        except Exception:
            logger.exception("get_danmaku 异常 (cid=%s)", cid)
            return []

    async def get_cid(self, bvid: str) -> int:
        """获取视频第一P的 cid。"""
        client = await self._get_client()
        try:
            resp = await client.get(
                f"{self.BASE}/x/player/pagelist",
                params={"bvid": bvid},
            )
            pages = resp.json().get("data", [])
            return pages[0].get("cid", 0) if pages else 0
        except Exception:
            logger.exception("get_cid 异常")
            return 0

    # ── 标签 ──

    async def get_video_tags(self, bvid: str) -> list[str]:
        """获取视频标签。"""
        client = await self._get_client()
        try:
            resp = await client.get(
                f"{self.BASE}/x/tag/archive/tags",
                params={"bvid": bvid},
            )
            data = resp.json()
            if data.get("code") != 0:
                return []
            return [t["tag_name"] for t in data.get("data", [])]
        except Exception:
            return []

    # ── AI 字幕摘要 (B站自带，可选) ──

    async def get_ai_summary(self, bvid: str, aid: int, cid: int = 0) -> str:
        """尝试获取B站自己的 AI 视频摘要（不保证有）。"""
        client = await self._get_client()
        try:
            # 先获取 cid
            if cid == 0:
                resp = await client.get(
                    f"{self.BASE}/x/player/pagelist",
                    params={"bvid": bvid},
                )
                pages = resp.json().get("data", [])
                if pages:
                    cid = pages[0].get("cid", 0)
            if cid == 0:
                return ""
            resp = await client.get(
                f"{self.BASE}/x/web-interface/view/conclusion/get",
                params={
                    "bvid": bvid,
                    "cid": cid,
                    "up_mid": "",
                },
            )
            data = resp.json()
            if data.get("code") == 0:
                model_result = data.get("data", {}).get("model_result", {})
                summary = model_result.get("summary", "")
                if summary:
                    return summary
            return ""
        except Exception:
            logger.exception("get_ai_summary 异常")
            return ""

    # ── @提及 消息列表 ──

    async def get_at_messages(self, last_id: int = 0) -> list[MentionItem]:
        """获取 @我 的消息列表。

        Args:
            last_id: 上次已处理的最大消息 id，只返回比它更新的消息。
        """
        client = await self._get_client()
        items: list[MentionItem] = []
        try:
            resp = await client.get(
                f"{self.BASE}/x/msgfeed/at",
                params={"build": 0, "mobi_app": "web"},
            )
            data = resp.json()
            if data.get("code") != 0:
                logger.warning("获取@消息失败: %s", data.get("message"))
                return items
            at_items = data.get("data", {}).get("items", [])
            for it in at_items:
                msg_id = it.get("id", 0)
                if msg_id <= last_id:
                    continue
                user = it.get("user", {})
                item = it.get("item", {})
                items.append(
                    MentionItem(
                        id=msg_id,
                        user_name=user.get("nickname", ""),
                        user_mid=user.get("mid", 0),
                        item_type=item.get("type", ""),
                        source_id=item.get("source_id", 0),
                        root_id=item.get("root_id", 0),
                        target_id=item.get("target_id", 0),
                        subject_id=item.get("subject_id", 0),
                        uri=item.get("uri", ""),
                        native_uri=item.get("native_uri", ""),
                        title=item.get("title", ""),
                        source_content=item.get("source_content", ""),
                        at_time=it.get("at_time", 0),
                    )
                )
            # 按 id 升序，先处理旧的
            items.sort(key=lambda x: x.id)
        except Exception:
            logger.exception("get_at_messages 异常")
        return items

    # ── 发表评论/回复 ──

    async def reply_comment(
        self,
        oid: int,
        root: int,
        parent: int,
        message: str,
        type_: int = 1,  # 1=视频
    ) -> bool:
        """回复一条评论。

        Args:
            oid:     评论区 oid（视频则为 aid）
            root:    根评论 rpid
            parent:  父评论 rpid（直接回复 root 时 = root）
            message: 回复正文
            type_:   评论区类型（1=视频, 12=专栏, 17=动态...）
        """
        client = await self._get_client()
        try:
            resp = await client.post(
                f"{self.BASE}/x/v2/reply/add",
                data={
                    "oid": oid,
                    "type": type_,
                    "root": root,
                    "parent": parent,
                    "message": message,
                    "csrf": self.auth.bili_jct,
                },
            )
            data = resp.json()
            if data.get("code") == 0:
                rpid = data.get("data", {}).get("rpid", "?")
                logger.info("回复成功: oid=%s root=%s rpid=%s", oid, root, rpid)
                return True
            else:
                logger.warning(
                    "回复失败: code=%s msg=%s | oid=%s root=%s parent=%s",
                    data.get("code"), data.get("message"), oid, root, parent,
                )
                return False
        except Exception:
            logger.exception("reply_comment 异常")
            return False
