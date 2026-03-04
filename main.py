"""
B站视频总结 Bot —— 主入口

检测评论区 @提及 → 总结视频内容 → 自动回复
"""

from __future__ import annotations

import asyncio
import logging
import random
import sys
import time

from bilibili.api import BiliAPI, MentionItem
from bilibili.auth import BiliAuth
from config import Config
from monitor.mention import MentionMonitor
from summarizer.video import LLMProfile, VideoSummarizer

# ── 日志 ──

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("bot.log", encoding="utf-8"),
    ],
)
logger = logging.getLogger("main")


# ── 回调：处理一条 @消息 ──


HEART_EMOJIS = ['❤️', '🧡', '💛', '💚', '💙', '💜', '🤍', '🤎', '💗', '💖', '💝']


def first_char_teacher(name: str) -> str:
    """取用户名第一个字 + '老师'，如 '卡洛琳爱' → '卡老师'，'Alice' → 'A老师'"""
    return (name[0] if name else "你") + "老师"


async def handle_mention(
    api: BiliAPI,
    mention: MentionItem,
    summarizer: VideoSummarizer,
    bot_name: str,
) -> None:
    """收到 @消息后的处理流程。"""

    # 1) 从 URI 提取 BV 号
    bvid = api.extract_bvid(mention.uri) or api.extract_bvid(mention.native_uri)

    # 如果 URI 是动态(opus)链接，尝试从动态中提取关联视频
    if not bvid:
        opus_id = api.extract_opus_id(mention.uri) or api.extract_opus_id(mention.native_uri)
        if opus_id:
            logger.info("检测到动态链接，尝试提取视频: opus_id=%s", opus_id)
            bvid = await api.get_bvid_from_opus(opus_id)

    if not bvid:
        logger.info("消息不包含视频，跳过: uri=%s", mention.uri)
        return

    logger.info("开始总结视频: %s (来自 @%s)", bvid, mention.user_name)

    # 2) 获取视频信息，拿到真实 aid 用于回复
    video_info = await api.get_video_info(bvid)
    if not video_info:
        logger.warning("无法获取视频信息，跳过: %s", bvid)
        return

    # 3) 根据用户评论内容选择 LLM
    profile = summarizer.resolve_profile(mention.source_content)

    # 4) 生成总结（计时）
    #    先预估 header/footer 占用字数，让总结的有效长度更精确
    teacher = first_char_teacher(mention.user_name)
    heart = random.choice(HEART_EMOJIS)
    #    header 最长情况（召唤模型）约 50 字，footer 约 15 字，加换行
    overhead = 70

    t0 = time.monotonic()
    summary = await summarizer.summarize(bvid, profile=profile, overhead=overhead)
    elapsed = time.monotonic() - t0
    if elapsed < 0.01:
        elapsed = 0.01  # fallback

    if not summary:
        summary = f"抱歉，暂时无法总结这个视频（{bvid}），可能是视频信息获取失败 😢"

    # 5) 组装回复
    if profile != summarizer.default_profile:
        header = (
            f"{bot_name}召唤了{profile.display_name}模型，"
            f"用时{elapsed:.2f}秒，为{teacher}总结如下："
        )
    else:
        header = (
            f"{bot_name} 花了 {elapsed:.2f}秒 看完了视频，"
            f"为{teacher}总结如下："
        )
    reply_text = (
        f"{header}\n"
        f"{summary}\n"
        f"记得常找{bot_name}来玩哦！{heart}"
    )

    # 6) 确定回复参数
    #    oid = subject_id = 视频的 aid
    #    source_id = 评论的 rpid（用户 @ 我的那条评论）
    #    root: 如果 root_id != 0 说明是楼中楼，否则 source_id 就是根评论
    oid = video_info.aid
    root = mention.root_id if mention.root_id != 0 else mention.source_id
    parent = mention.source_id

    logger.info(
        "回复参数: oid(aid)=%s root=%s parent=%s source_id=%s subject_id=%s",
        oid, root, parent, mention.source_id, mention.subject_id,
    )

    # 6) 发送回复
    ok = await api.reply_comment(
        oid=oid,
        root=root,
        parent=parent,
        message=reply_text,
    )
    if ok:
        logger.info("✅ 已回复 @%s (视频 %s)", mention.user_name, bvid)
    else:
        logger.error("❌ 回复失败 @%s (视频 %s)", mention.user_name, bvid)


# ── 入口 ──


async def main():
    logger.info("=" * 50)
    logger.info("B站视频总结 Bot 启动中...")
    logger.info("=" * 50)

    # 加载配置
    try:
        cfg = Config.from_env()
    except EnvironmentError as e:
        logger.error("配置错误: %s", e)
        logger.error("请复制 .env.example 为 .env 并填入正确的值")
        sys.exit(1)

    # 初始化组件
    auth = BiliAuth(cfg.sessdata, cfg.bili_jct, cfg.dedeuserid)
    api = BiliAPI(auth)

    default_profile = LLMProfile(
        name="DeepSeek",
        base_url=cfg.llm_base_url,
        api_key=cfg.llm_api_key,
        model=cfg.llm_model,
    )

    extra_profiles: dict[str, LLMProfile] = {}
    if cfg.claude_api_key:
        extra_profiles["claude"] = LLMProfile(
            name="Claude",
            base_url=cfg.claude_base_url,
            api_key=cfg.claude_api_key,
            model=cfg.claude_model,
            display_name="Claude Sonnet 4.0",
        )

    if cfg.openai_api_key:
        for keyword in ("chatgpt", "openai", "gpt"):
            extra_profiles[keyword] = LLMProfile(
                name="OpenAI",
                base_url=cfg.openai_base_url,
                api_key=cfg.openai_api_key,
                model=cfg.openai_model,
                display_name="GPT-4o",
            )

    summarizer = VideoSummarizer(
        api=api,
        default_profile=default_profile,
        extra_profiles=extra_profiles,
        max_length=cfg.max_reply_length,
        whisper_api_key=cfg.whisper_api_key if cfg.whisper_enabled else "",
        whisper_base_url=cfg.whisper_base_url,
        whisper_model=cfg.whisper_model,
        whisper_max_duration=cfg.whisper_max_duration,
    )

    # 创建带 summarizer 绑定的回调
    bot_name = cfg.bot_name

    async def on_mention(api_: BiliAPI, mention: MentionItem):
        await handle_mention(api_, mention, summarizer, bot_name)

    monitor = MentionMonitor(
        api=api,
        callback=on_mention,
        poll_interval=cfg.poll_interval,
    )

    logger.info("Bot UID: %s", cfg.dedeuserid)
    logger.info("LLM 模型: %s", cfg.llm_model)
    logger.info("Whisper 语音识别: %s (最大 %d 秒)", "开启" if cfg.whisper_enabled else "关闭", cfg.whisper_max_duration)
    logger.info("轮询间隔: %ds", cfg.poll_interval)
    logger.info("Bot 已就绪，开始监听 @消息...")

    try:
        await monitor.run()
    except KeyboardInterrupt:
        logger.info("收到退出信号，正在关闭...")
    finally:
        await api.close()
        logger.info("Bot 已停止。")


if __name__ == "__main__":
    asyncio.run(main())
