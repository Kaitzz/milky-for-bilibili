# B站视频总结 Bot 🤖

在 B站评论区 **@你的 Bot 账号**，自动总结视频内容并回复。

## 工作流程

```
用户在评论区 @Bot账号
       ↓
Bot 轮询 @消息接口，发现新提及
       ↓
从消息中提取视频 BV 号
       ↓
获取视频信息（标题/简介/字幕/标签）
       ↓
优先使用B站自带AI摘要，否则调用 LLM 生成总结
       ↓
自动回复到评论区，@回提及者
```

## 项目结构

```
video-summarizer/
├── .env.example          # 环境变量模板
├── requirements.txt      # Python 依赖
├── config.py             # 配置加载
├── main.py               # 入口
├── bilibili/
│   ├── auth.py           # Cookie 认证管理
│   └── api.py            # B站 API 封装
├── monitor/
│   └── mention.py        # @提及 轮询监控
└── summarizer/
    └── video.py          # 视频内容总结（LLM）
```

## 快速开始

### 1. 安装依赖

```bash
cd video-summarizer
pip install -r requirements.txt
```

### 2. 配置环境变量

```bash
cp .env.example .env
```

编辑 `.env` 文件，填入以下信息：

#### B站 Cookie（必填）

1. 浏览器登录 B站
2. F12 打开开发者工具 → Application → Cookies → `https://www.bilibili.com`
3. 复制以下三个值：

| Cookie 名 | 环境变量 |
|-----------|---------|
| `SESSDATA` | `BILIBILI_SESSDATA` |
| `bili_jct` | `BILIBILI_BILI_JCT` |
| `DedeUserID` | `BILIBILI_DEDEUSERID` |

> ⚠️ Cookie 有效期约 1 个月，过期后需重新获取。

#### Bot UID

`BOT_UID` 填写你 Bot 账号的数字 UID（即 `DedeUserID` 的值）。

#### LLM API（必填）

支持任何 OpenAI 兼容接口：

| 服务 | `LLM_BASE_URL` | 备注 |
|------|----------------|------|
| OpenAI | `https://api.openai.com/v1` | 推荐 `gpt-4o-mini` |
| DeepSeek | `https://api.deepseek.com` | 推荐 `deepseek-chat`，性价比高 |
| 本地 Ollama | `http://localhost:11434/v1` | 免费，需本地运行模型 |

### 3. 启动 Bot

```bash
python main.py
```

看到以下输出表示启动成功：

```
B站视频总结 Bot 启动中...
Bot 已就绪，开始监听 @消息...
```

## 总结策略

Bot 按以下优先级获取视频内容进行总结：

1. **B站自带 AI 摘要**（如果视频有的话，直接使用，无需调用 LLM）
2. **CC 字幕** → 发送给 LLM 生成总结
3. **标题 + 简介 + 标签** → 发送给 LLM 生成总结（信息较少，质量一般）

## 配置参数说明

| 环境变量 | 默认值 | 说明 |
|---------|-------|------|
| `POLL_INTERVAL` | `30` | 轮询间隔（秒），建议 ≥20 避免频繁请求 |
| `MAX_REPLY_LENGTH` | `800` | 回复最大字数（B站评论上限约 1000 字） |
| `LLM_MODEL` | `gpt-4o-mini` | LLM 模型名称 |

## 注意事项

1. **账号安全**：Bot 会自动发评论，频率过高可能触发B站风控，建议轮询间隔 ≥30s
2. **Cookie 过期**：SESSDATA 约 1 个月过期，需定期更新
3. **字幕依赖**：大部分B站视频没有 CC 字幕，此时只能根据标题和简介总结
4. **评论限制**：B站对新账号 / 低等级账号有评论频率限制
5. **合规风险**：频繁自动回复可能被B站判定为垃圾评论，建议小规模使用

## 后续可扩展

- [ ] 支持动态（type=17）的 @消息
- [ ] 接入 Whisper 等语音识别，直接从视频音频提取文本
- [ ] 支持识别评论中粘贴的 BV 号（不只是视频下方的评论）
- [ ] 添加 Web 管理面板，查看统计和日志
- [ ] 添加用户黑名单 / 白名单机制
- [ ] Docker 部署支持
