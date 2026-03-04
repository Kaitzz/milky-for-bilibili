"""
配置加载模块
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from dotenv import load_dotenv

load_dotenv()


@dataclass
class Config:
    # B站
    sessdata: str
    bili_jct: str
    dedeuserid: str
    bot_uid: int

    # LLM (默认)
    llm_base_url: str
    llm_api_key: str
    llm_model: str

    # LLM (Claude，可选)
    claude_base_url: str
    claude_api_key: str
    claude_model: str

    # LLM (OpenAI，可选)
    openai_base_url: str
    openai_api_key: str
    openai_model: str

    # Bot
    bot_name: str
    poll_interval: int
    max_reply_length: int

    @classmethod
    def from_env(cls) -> "Config":
        def _require(key: str) -> str:
            val = os.getenv(key, "").strip()
            if not val:
                raise EnvironmentError(f"缺少必需的环境变量: {key}")
            return val

        return cls(
            sessdata=_require("BILIBILI_SESSDATA"),
            bili_jct=_require("BILIBILI_BILI_JCT"),
            dedeuserid=_require("BILIBILI_DEDEUSERID"),
            bot_uid=int(_require("BOT_UID")),
            llm_base_url=os.getenv("LLM_BASE_URL", "https://api.deepseek.com"),
            llm_api_key=_require("LLM_API_KEY"),
            llm_model=os.getenv("LLM_MODEL", "deepseek-chat"),
            claude_base_url=os.getenv("CLAUDE_BASE_URL", "https://api.anthropic.com/v1/"),
            claude_api_key=os.getenv("CLAUDE_API_KEY", ""),
            claude_model=os.getenv("CLAUDE_MODEL", "claude-sonnet-4-20250514"),
            openai_base_url=os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1"),
            openai_api_key=os.getenv("OPENAI_API_KEY", ""),
            openai_model=os.getenv("OPENAI_MODEL", "gpt-4o"),
            bot_name=os.getenv("BOT_NAME", "总结姬"),
            poll_interval=int(os.getenv("POLL_INTERVAL", "30")),
            max_reply_length=int(os.getenv("MAX_REPLY_LENGTH", "800")),
        )
