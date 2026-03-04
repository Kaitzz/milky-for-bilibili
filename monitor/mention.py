"""
@提及 监控模块 —— 轮询 B站 @消息，分发给总结 & 回复流程
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from pathlib import Path

from bilibili.api import BiliAPI, MentionItem

logger = logging.getLogger(__name__)

# 持久化已回复 ID，防止重启后重复回复
# 支持通过 STATE_DIR 环境变量指定存储目录（用于 Railway Volume 挂载）
_state_dir = os.environ.get("STATE_DIR", "")
if _state_dir:
    STATE_FILE = Path(_state_dir) / "replied_comments.json"
else:
    STATE_FILE = Path(__file__).resolve().parent.parent / "replied_comments.json"


class MentionMonitor:
    """轮询 @我 的消息，筛选出评论区提及并回调处理函数。"""

    def __init__(
        self,
        api: BiliAPI,
        callback,  # async def callback(api, mention: MentionItem) -> None
        poll_interval: int = 30,
    ):
        self.api = api
        self.callback = callback
        self.poll_interval = poll_interval
        self._last_id: int = 0
        self._replied: set[int] = set()
        # 额外记录 (user_mid, subject_id) 对，防止对同一用户+同一视频重复回复
        self._replied_pairs: set[str] = set()
        self._cold_start: bool = False  # 是否冷启动（无状态文件）
        self._load_state()

    # ── 持久化 ──

    def _load_state(self):
        if STATE_FILE.exists():
            try:
                data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
                self._last_id = data.get("last_id", 0)
                self._replied = set(data.get("replied", []))
                self._replied_pairs = set(data.get("replied_pairs", []))
                self._cold_start = False
                logger.info(
                    "加载状态: last_id=%d, 已回复 %d 条, 去重对 %d 组",
                    self._last_id,
                    len(self._replied),
                    len(self._replied_pairs),
                )
            except Exception:
                logger.exception("加载状态文件失败")
                self._cold_start = True
        else:
            # 无状态文件 = 冷启动，跳过所有已有消息
            self._cold_start = True
            logger.warning("未找到状态文件，冷启动模式：将跳过所有已有 @消息")

    def _save_state(self):
        try:
            # 只保留最近 5000 条记录 / 3000 对，避免文件无限增长
            data = {
                "last_id": self._last_id,
                "replied": list(self._replied)[-5000:],
                "replied_pairs": list(self._replied_pairs)[-3000:],
            }
            STATE_FILE.write_text(
                json.dumps(data, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except Exception:
            logger.exception("保存状态文件失败")

    @staticmethod
    def _make_pair_key(mention: MentionItem) -> str:
        """生成 user+video 去重 key。"""
        return f"{mention.user_mid}:{mention.subject_id}"

    # ── 主循环 ──

    async def run(self):
        """启动轮询主循环。"""
        logger.info(
            "🤖 Mention Monitor 启动 | 轮询间隔 %ds | last_id=%d | 冷启动=%s",
            self.poll_interval,
            self._last_id,
            self._cold_start,
        )

        # 冷启动保护：首次拉取所有消息但不处理，只标记为已读
        if self._cold_start:
            await self._skip_existing()
            self._cold_start = False

        while True:
            try:
                await self._poll_once()
            except Exception:
                logger.exception("轮询出现未捕获异常")
            await asyncio.sleep(self.poll_interval)

    async def _skip_existing(self):
        """冷启动时：获取当前所有 @消息，标记为已处理但不回复。"""
        logger.info("⏭️ 冷启动：正在扫描已有 @消息并跳过…")
        mentions = await self.api.get_at_messages(last_id=0)
        skipped = 0
        for m in mentions:
            if m.id > self._last_id:
                self._last_id = m.id
            self._replied.add(m.id)
            self._replied_pairs.add(self._make_pair_key(m))
            skipped += 1
        self._save_state()
        logger.info("⏭️ 冷启动完成：跳过了 %d 条已有消息，last_id=%d", skipped, self._last_id)

    async def _poll_once(self):
        mentions = await self.api.get_at_messages(last_id=self._last_id)
        if not mentions:
            return

        logger.info("获取到 %d 条新 @消息", len(mentions))

        for m in mentions:
            # 更新游标
            if m.id > self._last_id:
                self._last_id = m.id

            # 跳过已处理（按消息 ID）
            if m.id in self._replied:
                continue

            # 跳过同一用户对同一视频的重复请求
            pair_key = self._make_pair_key(m)
            if pair_key in self._replied_pairs:
                logger.info(
                    "跳过重复请求: user=%s video=%s (已回复过)",
                    m.user_name, m.subject_id,
                )
                self._replied.add(m.id)
                self._save_state()
                continue

            # 只处理评论区 @（type = "reply"）
            if m.item_type != "reply":
                logger.debug("跳过非评论 @消息: type=%s id=%d", m.item_type, m.id)
                self._replied.add(m.id)
                self._save_state()
                continue

            logger.info(
                "处理 @消息: id=%d user=%s title=%s",
                m.id,
                m.user_name,
                m.title,
            )

            try:
                await self.callback(self.api, m)
            except Exception:
                logger.exception("处理 @消息 id=%d 时出错", m.id)

            self._replied.add(m.id)
            self._replied_pairs.add(pair_key)
            self._save_state()
