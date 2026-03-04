"""
B站视频总结 Bot —— 主入口

检测评论区 @提及 → 总结视频内容 → 自动回复
支持邮件发送 Markdown 笔记
"""

from __future__ import annotations

import asyncio
import logging
import random
import re
import sys
import time

from bilibili.api import BiliAPI, MentionItem
from bilibili.auth import BiliAuth
from config import Config
from monitor.mention import MentionMonitor
from services.email_sender import EmailSender
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


EMAIL_KEYWORDS = ("邮箱", "email", "邮件", "笔记发我", "笔记发给我", "发笔记")
EMAIL_PATTERN = re.compile(r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}")


def wants_email(text: str) -> bool:
    """判断用户评论是否请求邮件发送笔记。"""
    lower = text.lower()
    return any(kw in lower for kw in EMAIL_KEYWORDS)


async def find_email_in_dm(api: BiliAPI, user_mid: int, timeout: int = 180) -> str | None:
    """在用户私信中查找邮箱地址，最多等待 timeout 秒。"""
    deadline = time.monotonic() + timeout
    poll_interval = 15  # 每 15 秒检查一次

    while time.monotonic() < deadline:
        messages = await api.fetch_dm_messages(user_mid, size=10)
        for msg in messages:
            if msg["sender_uid"] == user_mid:
                match = EMAIL_PATTERN.search(msg["content"])
                if match:
                    return match.group(0)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        await asyncio.sleep(min(poll_interval, remaining))

    return None


async def handle_email_request(
    api: BiliAPI,
    mention: MentionItem,
    summarizer: VideoSummarizer,
    email_sender: EmailSender,
    bot_name: str,
    bvid: str,
    video_title: str,
    video_aid: int,
) -> None:
    """处理用户的邮件笔记请求。"""
    user_mid = mention.user_mid
    teacher = first_char_teacher(mention.user_name)
    heart = random.choice(HEART_EMOJIS)

    logger.info("📧 收到邮件笔记请求: %s (来自 @%s)", bvid, mention.user_name)

    # 1) 先在私信里找邮箱
    logger.info("检查用户 %s 的私信是否已包含邮箱…", user_mid)
    email = await find_email_in_dm(api, user_mid, timeout=10)

    # 2) 如果没找到，主动私信用户并等待
    if not email:
        prompt_msg = (
            f"Milky 已经接到您的指令啦~ 🎉\n"
            f"请发送您的 email 地址，"
            f"Milky 会将整理好的笔记文件发送至您的邮箱哦！"
        )
        sent = await api.send_dm(user_mid, prompt_msg)
        if not sent:
            logger.warning("无法私信用户 %s，跳过邮件流程", user_mid)
            # 在评论区提示用户
            await _reply(api, mention, video_aid,
                         f"抱歉{teacher}，Milky 无法发送私信给你，"
                         f"请确认你的私信设置允许陌生人消息哦 😢{heart}")
            return

        logger.info("已私信用户 %s，等待邮箱地址 (最多3分钟)…", user_mid)
        email = await find_email_in_dm(api, user_mid, timeout=180)

    if not email:
        logger.warning("用户 %s 未在 3 分钟内提供邮箱", user_mid)
        await _reply(api, mention, video_aid,
                     f"{teacher}，Milky 等了 3 分钟没有收到你的邮箱地址哦 😢 "
                     f"下次记得先私信邮箱给 Milky 再召唤哦~{heart}")
        return

    logger.info("获取到邮箱: %s (用户 %s)", email, user_mid)

    # 3) 选择 LLM 并生成 Markdown 笔记
    profile = summarizer.resolve_profile(mention.source_content)

    t0 = time.monotonic()
    notes, used_asr = await summarizer.generate_notes(bvid, profile=profile)
    elapsed = time.monotonic() - t0

    if not notes:
        await _reply(api, mention, video_aid,
                     f"抱歉{teacher}，Milky 暂时无法为这个视频生成笔记 😢{heart}")
        return

    logger.info("笔记生成完成: %d 字, 用时 %.1f 秒", len(notes), elapsed)

    # 4) 发送邮件
    ok = email_sender.send_notes(
        to_email=email,
        video_title=video_title,
        bvid=bvid,
        markdown_content=notes,
    )

    # 5) 在评论区回复结果
    if ok:
        asr_note = ""
        if used_asr:
            asr_note = f"\n本次笔记利用了SiliconFlow提供的语音技术与{profile.display_name}。"
        reply = (
            f"已将整理好的笔记文件发送至{teacher}的邮箱啦~ 📨✨"
            f"{asr_note}"
            f"\n记得随时呼叫{bot_name}哦！{heart}"
        )
    else:
        reply = (
            f"抱歉{teacher}，笔记已生成但邮件发送失败了 😢 "
            f"请检查邮箱地址是否正确，稍后再试~{heart}"
        )

    await _reply(api, mention, video_aid, reply)


async def _reply(api: BiliAPI, mention: MentionItem, aid: int, message: str) -> bool:
    """快捷回复评论。"""
    root = mention.root_id if mention.root_id != 0 else mention.source_id
    return await api.reply_comment(
        oid=aid,
        root=root,
        parent=mention.source_id,
        message=message,
    )


async def handle_mention(
    api: BiliAPI,
    mention: MentionItem,
    summarizer: VideoSummarizer,
    bot_name: str,
    email_sender: EmailSender | None = None,
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

    # 2.5) 检查是否请求邮件发送笔记
    if email_sender and wants_email(mention.source_content):
        await handle_email_request(
            api=api,
            mention=mention,
            summarizer=summarizer,
            email_sender=email_sender,
            bot_name=bot_name,
            bvid=bvid,
            video_title=video_info.title,
            video_aid=video_info.aid,
        )
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
    summary, used_asr = await summarizer.summarize(bvid, profile=profile, overhead=overhead)
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
    # 组装 footer
    if used_asr:
        asr_credit = f"\n本次总结利用了SiliconFlow提供的语音技术与{profile.display_name}。"
    else:
        asr_credit = ""
    footer = f"\n记得随时呼叫{bot_name}哦！{heart}"

    reply_text = f"{header}\n{summary}{asr_credit}{footer}"

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

    # 邮件发送器（可选）
    email_sender = None
    if cfg.email_enabled and cfg.smtp_host and cfg.smtp_user and cfg.smtp_password:
        email_sender = EmailSender(
            smtp_host=cfg.smtp_host,
            smtp_port=cfg.smtp_port,
            smtp_user=cfg.smtp_user,
            smtp_password=cfg.smtp_password,
        )
        logger.info("📧 邮件笔记功能: 开启 | SMTP: %s | 发件人: %s", cfg.smtp_host, cfg.smtp_user)
    else:
        logger.info("📧 邮件笔记功能: 关闭")

    async def on_mention(api_: BiliAPI, mention: MentionItem):
        await handle_mention(api_, mention, summarizer, bot_name, email_sender)

    monitor = MentionMonitor(
        api=api,
        callback=on_mention,
        poll_interval=cfg.poll_interval,
    )

    logger.info("Bot UID: %s", cfg.dedeuserid)
    logger.info("LLM 模型: %s", cfg.llm_model)
    if cfg.whisper_enabled:
        logger.info("Whisper 语音识别: 开启 | 服务: %s | 模型: %s | 最大 %d 秒", cfg.whisper_base_url, cfg.whisper_model, cfg.whisper_max_duration)
    else:
        logger.info("Whisper 语音识别: 关闭")
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
