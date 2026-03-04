"""
视频内容总结模块 —— 获取视频文本素材并调用 LLM 生成摘要
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from textwrap import dedent

from openai import AsyncOpenAI

from bilibili.api import BiliAPI, VideoInfo

logger = logging.getLogger(__name__)

# B 站评论单条上限约 1000 字，留一些余量给 @用户名 等
DEFAULT_MAX_LENGTH = 800

SYSTEM_PROMPT = dedent("""\
    你是B站评论区的视频总结助手，名叫 {bot_name}。请根据提供的视频素材
    （可能包含字幕、弹幕、B站AI摘要、标题、简介、标签等）生成一份
    高质量、有深度的中文总结。

    核心要求：
    1. 深入分析视频的核心观点、论据和结论，不要只列大纲
    2. 如果有字幕/弹幕，基于实际内容提炼关键信息和洞察
    3. 弹幕代表观众反应，可以从中提取观众关注点和共鸣点
    4. 如果 B站AI摘要 可用，将其作为参考但要用自己的方式重新组织、补充深度
    5. 适当使用 emoji 让内容更生动，但不要过度
    6. 用 3-6 个要点概括，每个要点需有实质内容（不是一句标题）
    7. 语气友好、自然，像一个知识渊博的朋友在分享
    8. 严格控制总字数在 {max_length} 字以内
    9. 不要使用 Markdown 格式（B站评论不支持）
    10. 开头直接进入内容，不要说"以下是总结"之类的话
    11. 不要在开头加主观评价或概括性引言
    12. 如果视频涉及英文/日文等外语内容，保留关键外语术语并附中文解释
    13. 技术类视频保留专业术语，科普类视频用通俗语言解释
    14. 如果素材很少（只有标题和简介），坦诚说明素材有限，尽力根据已有信息总结
    15. 回复中不要频繁提及"弹幕"二字（最多出现1-2次）。可用"观众反馈""网友热议""大家纷纷表示"等替代说法来引用观众观点
""")


@dataclass
class LLMProfile:
    """一组 LLM 配置（base_url + api_key + model）。"""
    name: str
    base_url: str
    api_key: str
    model: str
    display_name: str = ""  # 回复中展示的名字，如 "Claude Sonnet 4.0"

    def __post_init__(self):
        if not self.display_name:
            self.display_name = self.name

    def build_client(self) -> AsyncOpenAI:
        return AsyncOpenAI(base_url=self.base_url, api_key=self.api_key)


class VideoSummarizer:
    """获取视频素材 → 调用 LLM → 返回文本摘要。"""

    def __init__(
        self,
        api: BiliAPI,
        default_profile: LLMProfile,
        extra_profiles: dict[str, LLMProfile] | None = None,
        max_length: int = DEFAULT_MAX_LENGTH,
    ):
        self.api = api
        self.max_length = max_length
        self.default_profile = default_profile
        # name → profile, 用于按关键词切换
        self.profiles: dict[str, LLMProfile] = {}
        if extra_profiles:
            self.profiles.update(extra_profiles)

    def resolve_profile(self, user_text: str) -> LLMProfile:
        """根据用户评论内容选择 LLM。如果提到特定关键词就用对应模型。"""
        lower = user_text.lower()
        for keyword, profile in self.profiles.items():
            if keyword in lower:
                if profile.api_key:
                    logger.info("用户要求使用 %s", profile.name)
                    return profile
                else:
                    logger.warning("用户要求 %s 但未配置 API Key，回退默认", profile.name)
        return self.default_profile

    async def summarize(self, bvid: str, profile: LLMProfile | None = None, overhead: int = 0) -> str | None:
        """对一个视频生成总结文本。

        新策略：统一走 LLM，收集所有可用素材作为上下文：
          - CC 字幕（最优质素材）
          - 弹幕（观众实时反应，几乎所有视频都有）
          - B站 AI 摘要（作为辅助参考，不再直接返回）
          - 标签
          - 标题 + 简介 + UP主 + 时长
        """
        if profile is None:
            profile = self.default_profile

        info = await self.api.get_video_info(bvid)
        if info is None:
            logger.warning("无法获取视频信息: %s", bvid)
            return None

        # 并行获取所有素材
        cid = await self.api.get_cid(bvid)

        # 收集各类素材
        subtitle_text = await self._fetch_subtitle(info)
        danmaku_list = await self.api.get_danmaku(cid) if cid else []
        ai_summary = await self.api.get_ai_summary(bvid, info.aid, cid=cid)
        tags = await self.api.get_video_tags(bvid)

        # 记录素材情况
        sources = []
        if subtitle_text:
            sources.append(f"字幕({len(subtitle_text)}字)")
        if danmaku_list:
            sources.append(f"弹幕({len(danmaku_list)}条)")
        if ai_summary:
            sources.append("B站AI摘要")
        if tags:
            sources.append(f"标签({len(tags)})")
        logger.info(
            "视频素材: %s → %s",
            bvid,
            ", ".join(sources) if sources else "仅标题+简介",
        )

        # 有效摘要长度 = 总限制 - header/footer 开销
        effective_max = self.max_length - overhead if overhead else self.max_length

        return await self._llm_summarize(
            info,
            subtitle_text=subtitle_text,
            danmaku_list=danmaku_list,
            ai_summary=ai_summary,
            tags=tags,
            profile=profile,
            effective_max=effective_max,
        )

    # ── 内部方法 ──

    async def _fetch_subtitle(self, info: VideoInfo) -> str:
        """尝试下载字幕并返回拼接文本。"""
        if not info.subtitle_urls:
            return ""
        # 优先中文字幕
        target = info.subtitle_urls[0]
        for s in info.subtitle_urls:
            if "中文" in s.get("lang", "") or "zh" in s.get("lang", "").lower():
                target = s
                break
        url = target.get("url", "")
        if not url:
            return ""
        text = await self.api.get_subtitle_text(url)
        return text

    def _format_bili_summary(self, summary: str, info: VideoInfo) -> str:
        """格式化B站自带的 AI 摘要（现在仅用于构建 prompt，不直接返回）。"""
        available = self.max_length - 20
        if len(summary) > available:
            summary = summary[:available] + "……"
        return summary

    async def _llm_summarize(
        self,
        info: VideoInfo,
        subtitle_text: str = "",
        danmaku_list: list[str] | None = None,
        ai_summary: str = "",
        tags: list[str] | None = None,
        profile: LLMProfile | None = None,
        effective_max: int = 0,
    ) -> str | None:
        """调用 LLM 生成总结——整合所有可用素材。"""
        if profile is None:
            profile = self.default_profile

        # 构建用户 prompt
        parts: list[str] = []
        parts.append(f"视频标题：{info.title}")
        parts.append(f"UP主：{info.owner_name}")
        duration_min = info.duration // 60
        parts.append(f"时长：{duration_min} 分钟")
        if info.desc:
            parts.append(f"简介：{info.desc[:500]}")
        if tags:
            parts.append(f"标签：{'、'.join(tags[:10])}")

        # B站 AI 摘要作为参考
        if ai_summary:
            parts.append(f"\n【B站AI摘要（仅供参考，请用自己的方式重新总结）】\n{ai_summary[:2000]}")

        # 字幕：最重要的素材
        if subtitle_text:
            truncated = subtitle_text[:8000]
            if len(subtitle_text) > 8000:
                truncated += "\n……（字幕已截断）"
            parts.append(f"\n【字幕内容】\n{truncated}")

        # 弹幕：补充观众视角
        if danmaku_list:
            # 取前 200 条弹幕，拼接
            dm_sample = danmaku_list[:200]
            dm_text = " | ".join(dm_sample)
            # 弹幕预算约 2000 字
            if len(dm_text) > 2000:
                dm_text = dm_text[:2000] + "……"
            parts.append(f"\n【弹幕精选（观众实时评论，可从中了解观众关注点）】\n{dm_text}")

        user_msg = "\n".join(parts)

        summary_limit = effective_max if effective_max else self.max_length
        system_msg = SYSTEM_PROMPT.format(
            max_length=summary_limit,
            bot_name="视频总结助手",
        )

        try:
            client = profile.build_client()
            logger.info("调用 LLM: %s (%s)", profile.name, profile.model)
            response = await client.chat.completions.create(
                model=profile.model,
                messages=[
                    {"role": "system", "content": system_msg},
                    {"role": "user", "content": user_msg},
                ],
                max_tokens=1500,
                temperature=0.6,
            )
            result = response.choices[0].message.content or ""
            body = result.strip()
            # 清理 Markdown 残留（B站评论不支持）
            body = body.replace("**", "")
            body = body.replace("__", "")
            # 确保不超长：智能截断，在最近的句子边界处截断
            if len(body) > summary_limit:
                body = self._smart_truncate(body, summary_limit)
            return body
        except Exception:
            logger.exception("LLM 调用失败")
            return None

    @staticmethod
    def _smart_truncate(text: str, limit: int) -> str:
        """在句子边界处智能截断，避免切断句子中间。"""
        if len(text) <= limit:
            return text

        # 在 limit 位置往前找最近的句子结尾符
        cut = text[:limit]
        # 句子结尾：句号、感叹号、问号、换行
        best = -1
        for sep in ("。", "！", "？", "❗", "\n", "~", "）", ")"):
            pos = cut.rfind(sep)
            if pos > best:
                best = pos

        # 如果找到了合理的断点（至少保留 60% 内容）
        if best > limit * 0.6:
            return text[: best + 1].rstrip()

        # 找不到好的断点就硬截断
        return cut.rstrip() + "……"
