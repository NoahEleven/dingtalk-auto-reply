# 钉钉自动回复（dingtalk-auto-reply）

> 监控钉钉未读会话，单聊用 AI 以老板口吻自动回复，群聊 / 指定名单只发微信提醒（**不代发，防社死**）。

🔗 **项目主页 / 源码**：https://github.com/NoahEleven/dingtalk-auto-reply

---

## ✨ 功能特性

- **未读监控**：通过 `dws`（DingTalk Workspace CLI）轮询未读会话，**不依赖本机钉钉客户端**。
- **单聊 AI 代复**：用 CodeBuddy Agent SDK（模型 `hy3`）以「老板本人」口吻自动回复，拒绝自报家门 / 称呼对方为老板。
- **群聊只提醒**：仅 `@我` / `@all` 推微信通知，**绝不代发**（防止在群里乱说话社死）。
- **抢答防护**：发现未读后延迟 2 分钟 + 老板活跃检测，老板正在聊就绝不抢话。
- **图片识别**：单聊图片直接内联 AI 识别并代复；群聊 `@我` 的图片识别后补进微信通知。
- **微信通知**：代发 / 需人工处理的消息推送到老板微信（依赖 `weixinclaw-proactive-push` skill，未装则自动降级为仅日志）。
- **健壮常驻**：单实例锁、日志轮转、心跳保活、Windows Startup 看门狗崩溃自愈。

---

## 🔧 工作原理

```
钉钉未读 ──dws──▶ 拉未读会话
   │
   ├─ 单聊 ──▶ 活跃检测 + 延迟2min ──▶ AI 生成老板口吻回复 ──▶ dws 带引用回复 ──▶ 微信通知
   │
   └─ 群聊 ──▶ 仅 @我/@all ──▶ 图片识别(可选) ──▶ 微信通知(不代发)
```

关键设计见 [SKILL.md](SKILL.md)（给 AI agent 的完整说明，含踩坑记录）。

---

## 📦 安装部署

### 1. 前置依赖（目标机需具备）

| 依赖 | 说明 | 备注 |
|------|------|------|
| **dws CLI** | WorkBuddy 自带连接器 | 需 `dws patch chmod` 授权 `chat.message:list` / `chat.message:send` / `contact:search`，并加入系统 PATH |
| **codebuddy CLI** | WorkBuddy managed node 自带 | SDK 内部 spawn 它跑 agent |
| **codebuddy-agent-sdk** | Python SDK（唯一后端） | `pip install codebuddy-agent-sdk`，必须装到运行脚本的 python |
| **weixinclaw-proactive-push** | 微信推送（可选） | 未装则自动降级为仅日志 |

> ⚠️ **dws 必须在系统 PATH 上**：终端能敲出 `dws` 即说明就位。本技能 `gen_launcher.py` 会自动把 dws/node 目录追加进用户 PATH（幂等，无需手动配）。

### 2. 克隆并配置

```bash
git clone https://github.com/NoahEleven/dingtalk-auto-reply.git
cd dingtalk-auto-reply

# 填配置（真实身份写这里，.env 不分发）
cp .env.example .env
# 按需编辑 .env：群昵称、本人昵称、CodeBuddy API Key 等

# 自测（集成验证，不真发回复，但会真实调一次 SDK 生成）
"$PY" _validate.py
```

### 3. 常驻运行

**Windows（推荐 · Startup 启动器）**

```bash
# 生成 Startup .vbs 启动器（自带崩溃自愈看门狗，登录后自动运行）
"$PY" gen_launcher.py
```

**macOS / Linux**

```bash
nohup "$PY" dingtalk_unread_monitor.py > ~/.workbuddy/dingtalk_auto_daemon.log 2>&1 & disown
```

> `$PY` 指装了 `codebuddy-agent-sdk` 的 python（WorkBuddy managed：`%USERPROFILE%\.workbuddy\binaries\python\envs\default\Scripts\python.exe`）。

---

## ⚙️ 配置（`.env`，可选但推荐）

所有项均可选，留空 = 自动探测 / 默认。完整注释见 [.env.example](.env.example)，常用项：

| 变量 | 说明 | 默认 |
|------|------|------|
| `CODEBUDDY_API_KEY` | CodeBuddy API Key（无人值守推荐；留空则复用 CLI 已登录凭据） | 空 |
| `POLL_INTERVAL` | 轮询间隔（秒） | `10` |
| `DRY_RUN=1` | 只验证不真发 | 关闭 |
| `REPLY_DELAY_SEC` | 单聊代发前延迟窗口（秒） | `120` |
| `ACTIVE_WINDOW_SEC` | 老板活跃判定窗口（秒） | `300` |
| `GROUP_PUSH` | 群消息微信策略：`atme`/`all`/`off` | `atme` |
| `MENTION_NAMES` | 群 `@我` 昵称候选（你的昵称） | `老板` |
| `SKIP_SENDERS` | 不代发名单（家人 / 上级） | 空 |
| `AUTO_REPLY_IMAGE` | 单聊图片代复开关 | 开 |
| `VISION_API_KEY/BASE_URL/MODEL` | 图片识别后端（默认 hy3 零配置） | 空 |

---

## 🚀 使用

```bash
# 1) 先验证（不真发、不推微信）
DRY_RUN=1 "$PY" dingtalk_unread_monitor.py

# 2) 真实运行（代老板发钉钉 + 推微信）
"$PY" dingtalk_unread_monitor.py

# 3) 补发漏掉的单聊（监控曾宕机 / DRY_RUN 期间漏的）
"$PY" recover_missed.py

# 停止（Windows）
powershell -File stop_monitor.ps1
```

**诊断日志**：`~/.workbuddy/dingtalk_auto_debug.log`（运行）、`~/.workbuddy/dingtalk_auto_audit.jsonl`（审计）。每 ~120s 一行 `[heartbeat]` 即健康。

---

## 🔒 隐私说明

- **源码不含任何真实身份**：真实姓名、昵称、城市、`openDingTalkId` 等一律不硬编码，全部通过私密 `.env` 注入。源码默认值只保留中性词「老板」。
- **`.env` 不分发**：已被 `.gitignore` 忽略，切勿提交外发。换机时 `cp .env.example .env` 自行填写。
- **人设红线**：`secretary_system_prompt.txt` 明确禁止回复里输出任何系统标识 / 用户名 / 英文 ID，也禁止主动透露老板真实个人信息。

---

## 📁 文件结构

```
dingtalk-auto-reply/
├── SKILL.md                      # 完整说明（给 AI agent，含踩坑记录）
├── README.md                     # 本文件（给人类，上手部署）
├── dingtalk_unread_monitor.py    # 主脚本（轮询+生成+回复+推送）
├── gen_launcher.py              # 启动器生成器（本机生成 Startup .vbs）
├── secretary_system_prompt.txt   # 老板口吻人设
├── _validate.py                  # 自测脚本（集成验证，不真发）
├── stop_monitor.ps1             # 精确结束监控进程
├── recover_missed.py            # 漏发补发
├── .env.example                  # 配置模板（安全，无真实值）
└── .gitignore                    # 防误提交 .env
```

> 📌 `.vbs` 启动器不随仓库分发（机器专属胶水文件），由 `gen_launcher.py` 在本机生成。

---

## ⚠️ 免责声明

代老板自动发送钉钉消息是**高风险对外操作**。本技能已做抢答防护、质量网、审计日志等多重保险，但仍请：
- 首次部署务必先 `DRY_RUN=1` 验证；
- 把家人 / 上级放入 `SKIP_SENDERS` 不代发；
- 定期查看审计日志 `~/.workbuddy/dingtalk_auto_audit.jsonl`。

使用时即代表你已了解并承担相关风险。

---

## 📄 License

MIT —— 自由使用、修改、分发，但需保留版权声明与免责声明。
