---
name: dingtalk-auto-reply
description: 监控钉钉未读会话，单聊用 AI 以老板口吻自动回复，群聊/指定名单只发微信提醒（不代发，防社死）。覆盖 dws 未读接口、CodeBuddy Agent SDK 生成回复的 SERVER__PORT 端口冲突坑、自报家门坑、回复自己坑。当用户要求「钉钉自动回复/钉钉代回/监听钉钉未读+AI回复」时使用。
agent_created: true
---

# 钉钉自动回复（未读监控 → AI 代复 → 微信通知）

监控钉钉未读会话，单聊用 AI 以老板口吻自动回复，群聊/指定名单只发微信提醒（不代发，防社死）。

## 技能包含的文件（自包含，可直接移植）

```
dingtalk-auto-reply/
├── SKILL.md                      # 本说明
├── dingtalk_unread_monitor.py    # 主脚本（轮询+生成+回复+推送；含日志轮转/单实例锁/缓存清理/心跳）
├── gen_launcher.py              # 启动器生成器（本机自动生成 Startup .vbs，.vbs 不随包分发）
├── secretary_system_prompt.txt   # 老板口吻人设（SDK system_prompt 注入）
├── _validate.py                  # 自测脚本（集成验证，不真发回复）
├── stop_monitor.ps1             # 精确结束本脚本 python（仅停进程）
└── recover_missed.py            # 补发漏掉的消息（监控曾宕机/DRY_RUN 时，手动拉未读并代复）
```

移植：把整个目录复制到目标机的 `~/.workbuddy/skills/` 即可，无需改代码。

⚙️ **移植后必做一步：把 dws 加入系统 PATH**（根因与自动追加机制见下方「dws 依赖」段）。`dws` 不在 PATH → `get_unread` 恒空；换机部署先跑一次 `gen_launcher.py` 自动幂等追加（无需重登），新终端敲 `dws` 有输出即就位。

⚠️ **启动器 `.vbs` 不随包分发**：`.vbs` 是机器专属胶水文件（路径用 `%USERPROFILE%` 环境变量展开动态推导、无硬编码用户名，并备 `SpecialFolders("Profile")` / `%HOMEDRIVE%%HOMEPATH%` 兜底），被列入 `.gitignore`，**不会随仓库/打包上传**。换机或重新克隆后，启动器由 `gen_launcher.py` 在本机生成（见下方「Windows · Startup 启动器」），监控脚本首次运行也会自动自检并生成，无需手动复制 `.vbs`。

## 完整 pipeline（顺序不可乱）

1. `dws chat message list-unread-conversations --format json` → 只返回有未读的会话（天然过滤）。
   字段：`openConversationId`、`title`、`singleChat`(true=单聊)、`unreadPoint`、`lastMsgCreateAt`。
2. 单聊 → 用 `openConversationId` 拉最新一条消息：
   `dws chat message list --group <openConversationId> --time "<now>" --direction older --limit 5 --format json`
   ⚠️ **必须带 `--direction older`**：含义"从给定时间往更早拉"，配 `--time now` 即返回最新消息（newest-first）。
   不带 direction 时默认 older 且起始时间若早于消息 → 消息被过滤成空。
   单聊也能用 `--group <openConversationId>`（无需 `--user`/`--open-dingtalk-id`）。
3. 用 CodeBuddy Agent SDK 生成老板口吻回复（见下方「生成回复」）。
4. `dws chat message reply --conversation-id <cid> --ref-msg-id <openMessageId> --ref-sender <senderOpenDingTalkId> --text <回复> --yes` 带引用回复。
5. 微信推送：默认用 `~/.workbuddy/skills/weixinclaw-proactive-push/send.js`（仅文本，不转发媒体）。
   若该 skill 未安装，脚本**自动降级为仅日志**，不报错、不阻断主流程。

## 路径解析（可移植核心，勿硬编码用户名）

脚本不再写死任何用户名。外部二进制按以下优先级解析，**全部支持环境变量覆盖**：

| 用途 | 环境变量覆盖 | 自动探测位置 |
|------|-------------|--------------|
| dws CLI（直接路径·主路由） | `DWS_EXE` | `~/.workbuddy/binaries/node/cli-connector-packages/node_modules/dingtalk-workspace-cli/vendor/dws.exe` |
| dws CLI（cmd 兜底） | `DWS_CMD` | `~/.workbuddy/binaries/node/cli-connector-packages/dws.cmd` |
| codebuddy CLI | `CODEBUDDY_CMD` | `~/.workbuddy/binaries/node/versions/<最新版本>/codebuddy.cmd` |
| node | `NODE` | `~/.workbuddy/binaries/node/versions/<最新版本>/node.exe` |
| 微信推送脚本 | `WEIXIN_SEND_JS` | `~/.workbuddy/skills/weixinclaw-proactive-push/send.js` |

人设文件 `secretary_system_prompt.txt` 用脚本所在目录相对路径定位，随技能一起走。

可调环境变量：`DRY_RUN=1`（只验证不真发）、`POLL_INTERVAL=秒`（轮询间隔）。

## 隐私与身份配置（重要）

- **源码不含任何真实身份**：真实姓名、昵称、英文名、城市、`openDingTalkId` 等**一律不硬编码**在 `.py` / 人设 / `.env.example` 里，全部通过私密 `.env` 注入（`SELF_SENDERS`/`MENTION_NAMES`/`BOSS_DISPLAY_NAME`/`SELF_OPENDINGTALK_ID`）。源码默认值只保留中性词"老板"。
- **`.env` 不分发**：已被 `.gitignore` 忽略，切勿提交或随技能打包外发。换机时 `cp .env.example .env` 后自行填写。
- **人设红线**：`secretary_system_prompt.txt` 明确禁止在回复里输出任何系统标识/用户名/英文 ID（如把 `SZTSY` 当称呼），也禁止主动透露老板真实姓名/城市/公司等个人信息。

## 生成回复（CodeBuddy Agent SDK · 单路径）

`gen_reply(sender, content)` 调度逻辑（**仅 Python SDK 单路径**，不再有 subprocess 降级）：
- `_gen_reply_sdk_async`：`CodeBuddyAgentOptions(system_prompt=人设, model=hy3, env={SERVER__PORT:空闲端口, CODEBUDDY_INTERNET_ENVIRONMENT: 自动检测中国版填 internal 否则 public, CODEBUDDY_API_KEY: 设了才注入}, codebuddy_code_path=codebuddy.cmd, permission_mode=bypassPermissions)`。`asyncio.run(asyncio.wait_for(..., 180))` 同步调用，热启 8-16s 返回。设了 Key 则注入 env 走 Key 认证（底层 CLI 处理），否则复用 CLI 已登录凭据。
- SDK 未装 → `gen_reply` **直接返回空**（不代发，推微信转人工），不崩溃，也不降级到 subprocess。

**关键坑（必读，否则挂死/胡说）**：
1. **SERVER__PORT 端口冲突**（最致命）：codebuddy CLI 起 prewarm 本地 server 占 `SERVER__PORT`，硬编码 `18732` 撞 WorkBuddy 桌面应用 → `EADDRINUSE` 永远 0 字节/超时。✅ 每次生成用 `socket` 分配空闲端口注入 `env["SERVER__PORT"]`。
2. **默认是编码 agent 人格，会自报家门/调工具**：✅ 用 `CodeBuddyAgentOptions(system_prompt="<完整人设>")` **编程式参数注入**（不是 `append_system_prompt`，否则仍保留 agent 人格）。人设里写死"你是老板本人、禁止自我介绍、禁止称呼对方为老板、只输出回复正文"。编程式传参天然避免命令行 `-p` 经 `cmd /c` 传递多行文本时的换行截断/乱码（人设里的 emoji、`→` 都能完整传入）。
3. **别"回复自己"**：会话最新一条可能来自老板本人。启动时 `get_self_openid()` 缓存老板 `openDingTalkId`，`_is_self(sender, sender_open_id)` **优先用 openid 精确匹配**，openid 拿不到才回退 `SELF_SENDERS` 昵称关键字（私密 `.env` 配置，源码默认仅"老板"）。命中则跳过自动回复。
4. **输出解析——SDK 直接收结构化文本块**：`query()` 以 `async for` 流式返回消息对象，最终回复在 `AssistantMessage.TextBlock.content`，天然不带 prewarm 日志/工具噪音。✅ 直接拼接所有 `AssistantMessage` 的 `TextBlock.text` 即得原始回复，比 `-p --output-format json` 再 `json.loads` 更稳。SDK 调用是"跑完一次性返回"，冷启动 + hy3 首次可能 3-5 分钟才返回，`asyncio.wait_for(..., 180)` 超时给 **180s** 安全余量；超时返回空 → 走「不代发转人工」。
- **安全网统一**（防御纵深，别删）：`extract_reply()` 抽 `<reply>` 标签正文，`_looks_like_reply()` 反向拒掉空回复/反问/角色扮演/自称老板/助理口吻；质量不达标/超时/异常返回空 → 调用方走「不代发，转人工通知」。
- **SDK 其它坑（已解决）**：Windows 上 `codebuddy_code_path` 必须用 `codebuddy.cmd`（非无扩展名 `bin/codebuddy`，否则 `WinError 193`）；中国版 `env={"CODEBUDDY_INTERNET_ENVIRONMENT":"internal"}`；末尾 `ValueError: I/O operation on closed pipe` 是 Windows asyncio 清理警告，无害。

## 抢答防护（延迟窗口 + 老板活跃检测）

**问题**：发现单聊未读后立即代发，会抢老板自己的回答（老板正拿着手机准备回，AI 已代回）。

**方案**：发现未读不立即发，走「延迟 + 活跃检测」两步：
1. **活跃检测（发现即查）**：`owner_recently_active(cid)` 拉该会话最近消息，看老板最近 `ACTIVE_WINDOW_SEC`（默认 300s=5分钟）内是否在该会话发过消息（`SELF_OPEN_ID` 精确匹配）。活跃 → 跳过不代发（老板在跟，会自己回）。
2. **延迟窗口**：不活跃 → 进 `PENDING` 等 `REPLY_DELAY_SEC`（默认 120s=2分钟）。
   - 窗口内每轮（轮询间隔）持续检测老板是否活跃，活跃则取消代发。
   - 窗口到期再次确认仍不活跃 → 真正代发（`gen_reply`+`send_reply`+审计+微信通知）。
3. 老板在窗口内自己回了该会话 → 活跃检测命中 → 代发取消，**绝不抢答**。

**可调环境变量**（默认已配好）：
- `REPLY_DELAY_SEC=120`：延迟窗口时长（秒），给老板自己回的时间。
- `ACTIVE_WINDOW_SEC=300`：老板活跃判定窗口（秒），最近 N 秒在该会话发过消息算"正在跟"。

**效果**：既给老板充足回复时间，又从根上避免"老板正在聊却被 AI 抢话"。群聊/媒体无文本/生成失败仍走原"不代发转人工"逻辑，不受影响。

## 群聊推送过滤（默认只推 @我/@all）

**问题**：早期实现把**所有**群消息都推到微信——普通群刷屏、@别人的消息、特别关注的人发言全都会打扰老板。老板只关心"有人@我"这类真正重要的群消息。

**接口事实（已实测）**：
- `list-unread-conversations` 和 `chat message list` 的**单条消息对象里没有 `@我` 结构化字段**（`atUsers`/`atDingTalkIds` 都不返回）；群消息里 @ 某人只表现为内容前缀纯文本 `@昵称`（如 `@盘锦丽 已经试过了`）。
- dws 另有服务端权威接口 `chat message list-mentions --group <cid>`（只返回 @我 的消息），但空群返回无 `messages` 字段的空结果，结构不稳定，不适合做热路径每轮轮询。
- **钉钉"特别关注"联系人列表 dws 拿不到**，无法按"是否特别关注"精确过滤；因此用"是否 @我/@all"这个可识别维度来收敛群推送。

**方案（内容启发式，零额外 API 调用 + `GROUP_PUSH` 策略开关）**：
- `group_msg_is_at_me(content)` 判据：
  1. 内容含 `@所有人` / `@all` / `<@all>`（dws 发送占位）→ 必然含老板 → `True`；
  2. 内容以 `@昵称` 开头且昵称命中 `MENTION_NAMES` → `True`；
  3. 其它（普通群消息、@别人、特别关注发言、媒体无文本）→ `False`。
- **群聊微信推送按 `GROUP_PUSH` 策略**（仅作用于群消息；单聊代复+提醒不受影响）：
  - `atme`（**默认**）：仅 `at_me=True`（@我/@all）才推微信；其余群消息**静默跳过，不推**。
  - `all`：所有群消息都推（旧行为）。
  - `off`：群聊完全不推微信（连@我都不推）。
  - 命中推送的消息标签为 `🔔 钉钉新消息（群聊·有人@你）`，一眼区分。

**配置**：
- 老板群昵称候选 `MENTION_NAMES`（源码默认仅 `老板`；真实群昵称在私密 `.env` 配置），群昵称特殊时覆盖：`MENTION_NAMES="你的昵称,你的简称"`。
- 群推送策略：`GROUP_PUSH=atme`（默认）｜ `all` ｜ `off`。

**局限**：纯展示名匹配，若群里恰好有人昵称与你相同会误判（极罕见）；如需 100% 服务端权威，可改用 `list-mentions` 二次确认（见上"接口事实"），当前为成本/可靠性权衡选择启发式。

## 图片识别（群聊 @我 / 单聊图片补全内容）

**问题**：钉钉图片消息不是"无文本"——mediaId 内嵌在 content 里（`[图片消息](mediaId=@lQLP...)`），常带文字说明+@人。原逻辑把图片当"(图片/媒体消息)"丢弃，老板既看不到图里是什么，也修不掉"图片被误当纯文本代复"的隐患（content 含 mediaId 噪音，喂给 AI 会乱回）。

**实现**：
- `extract_media_ids(content)`：正则提取所有图片 mediaId（一条消息可能多张，最多取前 4 张）。
- `clean_text_for_ai(content)`：去掉 `[图片消息](mediaId=...)` 噪音，保留纯文字说明，作为 AI 上下文（修掉误代复 bug）。
- `download_images(...)`：调 `dws chat message download-media` 把图下到 `_media_cache/`（路径必须用 Windows 原生反斜杠绝对路径，否则底层 Go 组件误解析 `/c/` 导致落盘失败）。
- `describe_image(path)` + `describe_images(paths)`：**默认复用 hy3 内置多模态**（CodeBuddy Agent SDK 的 image 协议，把图 base64 内联进 SDK query，实测可准确读出文字/颜色/图形，文本落在 `AssistantMessage/TextBlock`，抽取方式与 `gen_reply` 一致）；若 `.env` 显式配 `VISION_API_KEY` 则改用外部 OpenAI 兼容视觉 API（可选更快/更便宜模型）。两条路径都返回中文描述。主要用于**群聊 @我 通知**和**单聊不代复时让老板知道图里是什么**。
- 群聊：**仅 @我 时**识别（避免群刷图烧视觉额度），结果补进微信通知 `🖼️ 图片内容：XXX`（`describe_images` 1 次调用）。
- 单聊**代复**：有图且 `AUTO_REPLY_IMAGE` 开启（默认开） → 把图以 image 协议**直接内联进 `gen_reply` 的 hy3 调用**（识别+代复 **1 次调用**完成，不再先 `describe` 再代复的二次调用）；失败/质量不达标仍走安全网不代发。单聊**不代复**（开关关，`AUTO_REPLY_IMAGE=0`）时则只走 `describe_images` 1 次拿描述补通知。

**降级**：CodeBuddy Agent SDK 不可用（`_SDK_AVAILABLE=False`）→ `VISION_ENABLED=False` → 不识别、不下载图片，微信通知里图片只显示 `(图片/媒体消息)`，**不报错、不阻断**。只要 SDK 可用即开箱即用识别，无需任何 key。

**配置**：视觉后端默认 hy3（零配置）；如需覆盖填 `VISION_API_KEY` / `VISION_BASE_URL` / `VISION_MODEL`（OpenAI 兼容协议，如 gpt-4o-mini）。单聊图片自动代复开关 `AUTO_REPLY_IMAGE`（默认开，设 `AUTO_REPLY_IMAGE=0` 可关闭）。

## 健壮性要点

- **日志心跳**：每 ~120s 打一行 `[heartbeat] alive, unread_now=N`，无未读也打——静默但健康，区别于"静默空转假死"（详见下方「读取日志」）。
- **启动静默 seed**：首轮把当前未读时间戳记进去重表，只对启动后新到的消息回复，不 retro 回复历史未读。
- **去重键用 `lastMsgCreateAt`**（媒体消息 `openMessageId` 可能为空，不能做去重依据）。
- `DRY_RUN=1` 环境变量：只生成+打印，不真发回复/不推微信，用于验证。
- 仅单聊自动回复；`SKIP_SENDERS` 可配置不代发的名单（家人/上级）。
- **生成失败/媒体消息不代发**：AI 生成失败、质量不达标（`_looks_like_reply` 拒绝）、或媒体消息无文本时，**绝不发兜底话术**（避免对方以为老板看到了其实没看到），只推微信「需手动处理」转人工。**单聊**即使质检拦下也会把 AI 生成的草稿原文一并带进微信通知（不代发到钉钉，但让老板看到草稿、便于手动补发）。
- **审计日志**：每次代发/跳过都写 `~/.workbuddy/dingtalk_auto_audit.jsonl`（每行一个 JSON：时间、会话、发件人、内容、回复、发送结果）。代老板发消息是高风险对外操作，必须可追溯。
- **发送成功判断**：`_dws_ok()` 解析 dws 返回的 JSON 看 `success`/`errcode`，空输出/含 error/errcode≠0 都判失败；不再用"不含 error 即成功"的弱判据。
- **启动 seed 不吞消息（防"毫无反应"）**：首轮把未读时间戳记进去重表（不 retro 回复历史），但对「24h 内到达」的单聊未读**发一次被动微信提醒（仅提醒、不代复）**——downtime 期间到达的消息重启时不再被静默吞掉。
- **单实例锁**：`~/.workbuddy/dingtalk_auto.lock` 存 PID（原子 `O_CREAT|O_EXCL` 创建，避免竞态），启动检测到锁里 PID 还活着则立刻退出（杜绝同条消息双发）；锁里是陈旧 PID（进程已死）则接管。
- **日志轮转**：`dingtalk_auto_debug.log` 超 1MB、`dingtalk_auto_audit.jsonl` 超 512KB 自动重命名为 `.1`（保留一份回溯），常驻进程下不再无限撑爆磁盘。
- **图片缓存清理**：每 200 轮自动清 `_media_cache/` 下超过 7 天的图片，防常驻下缓存无限增长。
- **主循环韧性**：外层 `try/except` 包裹整轮；`get_unread()` 等持续失败时按次数退避（每次 +10s、上限 120s）避免热循环狂刷日志；异常被捕获后下一轮继续，监控不会"死掉"。

## 运行

> **Python 环境**：必须用装了 `codebuddy-agent-sdk` 的 python。WorkBuddy 上即 default venv：
> Windows `%USERPROFILE%\.workbuddy\binaries\python\envs\default\Scripts\python.exe`；macOS/Linux `$HOME/.workbuddy/binaries/python/envs/default/bin/python3`（下文统称 `$PY`）
> 未装 `codebuddy-agent-sdk` 时脚本无法生成回复（走「不代发」逻辑），但不会崩溃。装：`$PY -m pip install codebuddy-agent-sdk`

```bash
# Windows
PY="$USERPROFILE/.workbuddy/binaries/python/envs/default/Scripts/python.exe"
# macOS / Linux
PY="$HOME/.workbuddy/binaries/python/envs/default/bin/python3"

# 0) 自测（集成验证，不真发回复；会真实调一次 SDK 生成）
"$PY" ~/.workbuddy/skills/dingtalk-auto-reply/_validate.py

# 1) 先验证（不发真实回复、不推微信）
DRY_RUN=1 "$PY" ~/.workbuddy/skills/dingtalk-auto-reply/dingtalk_unread_monitor.py

# 2) 真实运行（会代老板发钉钉 + 推微信）
"$PY" ~/.workbuddy/skills/dingtalk-auto-reply/dingtalk_unread_monitor.py
```

> **dws 环境变量**：监控本体走绝对路径，但为让你手动用 `dws` 及兜底 `DWS_CMD`，本技能 `gen_launcher.py` 会自动把 dws/node 目录追加进用户 PATH（详见下方「dws 依赖」段）；手动补 PATH 亦见该段。

### ⚠️ CodeBuddy 认证（启动前必读）

脚本**启动即做认证健康检查**，未通过会打印醒目提示并退出（exit 2），不会悄悄空跑：
- **API Key（推荐无人值守）**：`.env` 填 `CODEBUDDY_API_KEY=你的key`（申请：https://copilot.tencent.com 控制台）→ 用 Key 直连，不依赖交互登录。
- **CLI 已登录凭据（零配置）**：留空 Key，脚本自动复用你终端 `codebuddy` 登录过的凭据。
- 都没 → 退出码 2，提示二选一：①终端运行 `codebuddy` 完成登录；②在 `.env` 填 `CODEBUDDY_API_KEY`。
- 中国版自动适配：脚本检测 `~/.codebuddy/local_storage` 是否标记 `internal`，自动注入 `CODEBUDDY_INTERNET_ENVIRONMENT=internal`，无需手动配。

> 脚本**绝不代填/存储你的登录凭据**，只检测"是否已登录"并给提醒——认证动作由你本人在终端完成。首次启动前请先确认已登录（终端跑一次 `codebuddy` 或填 Key），未登录会打印提示并以退出码 2 退出，绝不代发。

## 部署方式

> WorkBuddy 桌面客户端不能作为后台常驻进程；但本脚本是独立 Python 进程，可自行后台常驻轮询。生成回复走 Python SDK（即起即退的子进程），不依赖 WorkBuddy 桌面客户端运行。

### Windows · Startup 启动器（推荐）

启动器 `.vbs` **不随技能分发**（见上方移植说明，已被 `.gitignore` 排除），由本机 `gen_launcher.py` 生成到 `%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\`。两种落地方式，任选其一（或都做）：

```bash
# 方式 A：手动生成一次（推荐，部署时执行）
"$PY" ~/.workbuddy/skills/dingtalk-auto-reply/gen_launcher.py
# 方式 B：什么都不做 —— 监控脚本首次运行会自动自检并生成（日志可见 [launcher] 行）
```

- 生成后双击即可手动拉起；放进「启动」文件夹则每次登录自动运行（生成器已写到该位置）。
- **自带崩溃自愈看门狗**：常驻循环每 30s 探活 `dingtalk_unread_monitor.py` 的 python——进程不在立刻拉起；进程在但调试日志超 180s 未更新（卡死/静默空转）则杀掉重拉，~30s 内自愈，无需重登。
- **单实例锁**（见上方「健壮性要点」）双重保险，重复启动自动退出，杜绝双发。
- 生成器以 **纯 ASCII** 写 `.vbs`（全英文注释），Windows Script Host 读取零风险。
- 看门狗逻辑唯一可信源即生成出的 `dingtalk_auto_reply_launcher.vbs`（由 `gen_launcher.py` 的 `VBS_CONTENT` 常量生成，改动请以改 `gen_launcher.py` 为准，勿手改运行中的 `.vbs` 以免漂移）。

> ⚠️ **为什么不用 Windows 计划任务**：计划任务把脚本跑在**隔离会话**里，该会话**无网络出口、也拿不到 WorkBuddy 宿主本地服务** → `dws` 连不上后端、每次 `get_unread` 超时返回空 → 日志只剩启动 banner、看不到 `raw unread`、不会代复（进程活着 CPU≈0 却不工作）。Startup 启动器在**登录后的交互式会话**（与 WorkBuddy 宿主同会话、有完整网络）拉起，从根上避开。故计划任务方案已弃用，不要再注册。

> 🔴 **窗口样式与 dws 访问无关（2026-07-15 老板纠偏）**：VBS 拉起监控的窗口样式 `N`（0=隐藏 / 1=普通 / 2=最小化）**不影响 dws 能否拿到未读**。能否拿到未读只取决于「dws 在系统 PATH」+「跑在登录后的交互式会话」（见上方「为什么不用 Windows 计划任务」与下方「dws 依赖」两段），与隐藏/最小化无关。之前"隐藏窗口会摘出会话导致 dws 返回空"是错误猜想，真实根因是 dws 当时没进 PATH。

### macOS / Linux

```bash
nohup python ~/.workbuddy/skills/dingtalk-auto-reply/dingtalk_unread_monitor.py \
  > ~/.workbuddy/dingtalk_auto_daemon.log 2>&1 & disown
```

### 手动 start / stop

- 启动：直接 `python dingtalk_unread_monitor.py`（前台 `python` 调试；正式运行由 VBS 启动器以 `python.exe` 隐藏窗口拉起）。
- 停止（Windows）：`stop_monitor.ps1`（精确结束本脚本 python，不动其它 python）。
- 停止（macOS/Linux）：`pkill -f dingtalk_unread_monitor.py` 按 PID 杀。

## dws 依赖（必须满足，不依赖钉钉客户端）

监控**只通过 `dws`（DingTalk Workspace CLI，WorkBuddy 连接器）** 读取未读、发送回复，
**不依赖本机钉钉桌面客户端**（dws 走 WorkBuddy 宿主本地服务，与是否在跑钉钉客户端无关）。
今天实测已石锤：dws 独立工作，之前「unread_now 恒为 0」的真实根因是 **dws 不在系统 PATH 上**，
而非客户端。需满足：
- **dws 已授权**（`dws patch chmod` 授权 `chat.message:list` / `chat.message:send` / `contact:search`）；
- **dws 在系统 PATH 上（User 环境变量）**：终端能敲出 `dws` 即说明已就位；本技能 `gen_launcher.py` 会**自动把 dws/node 目录追加进用户 PATH**（HKCU\\Environment，幂等、写后广播刷新），无需手动配；
- 监控进程跑在**登录后的交互式会话**（与 WorkBuddy 宿主同会话、有完整网络，而非计划任务/服务那种隔离会话）；
- `dws` 取不到未读（返回空/超时）的典型原因：**PATH 缺失**（dws 找不到）、会话隔离（无网）、未授权 → 监控"看着活着"却什么都不做。

  > ⚙️ **关于"读取空"的另一层根因（已实现方案，无需用户操作）**：dws 是 Node 打包二进制，对匿名管道(PIPE)的 stdout 是异步写，进程退出前未 flush → Python `capture_output` 读到 0 字节（与父进程是否控制台无关，python.exe 下同样复现）。监控**统一用文件重定向**（dws 输出写临时文件再读）绕过，每次稳定拿到数据；此实现细节已封装，用户只需保证上方 PATH / 会话 / 授权三条即可。

## 读取日志（诊断）

日志位置（监控以 `python.exe` 隐藏窗口运行，全部写文件，不落 stdout）：
- `~/.workbuddy/dingtalk_auto_debug.log` —— 运行日志（超 1MB 自动轮转为 `.1`）
- `~/.workbuddy/dingtalk_auto_audit.jsonl` —— 代发/跳过审计（超 512KB 轮转）

**如何判断监控健康**（`tail` 最后几行）：

| 日志现象 | 含义 | 处理 |
|---|---|---|
| 每 ~120s 一行 `[heartbeat] alive, unread_now=N, ts=HH:MM:SS` | 正常轮询中 | 无需处理 |
| 有 `raw unread: [...]` | 拉到未读会话了 | 正常，走代复/提醒流程 |
| 只有启动 banner、长时间无 `heartbeat`/`raw unread` | 卡死或掉进无网会话 | 看门狗应在 180s 内自动杀掉重拉；若仍无，检查钉钉客户端是否在跑、网络是否通 |
| `已有另一个监控实例在运行，本进程退出` | 单实例锁生效，重复启动被拒 | 正常，无需处理 |
| `auto-reply monitor started` | 新实例刚拉起 | 正常 |
| 审计日志出现 `skip_reply` / `notify_only` | 主动跳过（老板活跃 / 群非@我 / 生成失败） | 设计内，非 bug |

## 自测脚本 `_validate.py`

- 打印 4 个外部二进制/人设的解析路径（确认可移植探测正确）。
- **dws 实际调用比对**（每次自测必跑）：默认开启 `DEBUG_DWS_CALL=1`，把「配置的路径（DWS_EXE/DWS_ENTRY/DWS_CMD/NODE 是否存在）」与「`run_dws` 实际选用的路由（DIRECT-exe / NODE-direct / DWS_CMD-fallback / NONE）」和「接口真实返回的字节长度 / 解析出的会话数」三者对齐打印，并给 `PASS / WARN / FAIL` 结论；同时把每次 dws 调用的**真实 argv + 返回码 + stdout 长度**写进调试日志（`[dws-call]` / `[dws-route]` 行，数据抓取表现为 `[dws-file] OK out_len=N`）。三条路由输出统一走**文件重定向**（dws 对 PIPE 异步 flush 丢失、Python 读 PIPE 恒空，文件重定向是当前主力方案，与控制台/解释器无关）。排障"dws 找不到/调不通"时，先看这段比对 + 日志里的 argv，一眼定位是 PATH 缺失还是入口解析错。
- 调用 `get_unread` 看未读接口是否通；若有单聊未读，继续验证 `get_latest_msg` 字段、`gen_reply` 生成、`reply --dry-run` 命令形态。
- **不真发任何回复**（reply 用 `--dry-run`），但 `gen_reply` 会真实调一次 codebuddy 生成文本（仅一次、不发送）。
- 当前环境若无单聊未读（只有群聊），会优雅跳过单聊集成段——符合「群聊不代发」预期，不算失败。
- **`--test-guard`**：验证抢答防护逻辑（活跃检测 `owner_recently_active` + 延迟窗口参数），不真发、不触碰任何人会话。自动探测真实单聊 cid，打印活跃检测结果并演示"进窗口→到期代发"（DRY_RUN 下只生成不真发）。

## 漏发补发脚本 `recover_missed.py`

监控曾宕机、或历史某次以 `DRY_RUN=1` 启动过（只生成不真发）导致漏掉的单聊消息，用本脚本手动补发：拉取当前未读单聊 → 用 hy3 生成老板口吻回复 → **真正发送**并推微信通知。

```bash
# 补发所有漏掉的单聊未读（实时真发）
"$PY" ~/.workbuddy/skills/dingtalk-auto-reply/recover_missed.py
```

⚠️ 会真实代老板发消息，仅在你确认"确有漏发"时使用；群聊不代发（只通知），符合全局策略。

## 依赖（移植目标机需具备）

1. **dws CLI**（WorkBuddy 自带）：需授权 `chat.message:list`、`chat.message:send` 与 `contact:search`（后者用于获取本人 openid 以精确识别「回复自己」；用 `dws patch chmod` 授权）。
   - ⚙️ **必须在系统 PATH 上**（关键根因，勿漏）：机制与自动追加见上方「dws 依赖」段。
2. **codebuddy CLI**（WorkBuddy managed node 目录自带）：SDK 的 `codebuddy_code_path` 指向其 `codebuddy.cmd`，SDK 内部 spawn 它来跑 agent；模型 `hy3`。
3. **codebuddy-agent-sdk**（Python SDK，**唯一后端**）：`pip install codebuddy-agent-sdk`。**必须装到运行脚本的 python 环境**——WorkBuddy managed python 的 default venv（Windows：`%USERPROFILE%\.workbuddy\binaries\python\envs\default\Scripts\python.exe -m pip install codebuddy-agent-sdk`；macOS/Linux：`$HOME/.workbuddy/binaries/python/envs/default/bin/python -m pip install codebuddy-agent-sdk`）。未装则 `gen_reply` 直接返回空（不代发），不会崩溃。
4. **weixinclaw-proactive-push skill**（可选）：用于微信主动推送；未装则自动降级为仅日志。
5. 人设文件 `secretary_system_prompt.txt` 已随技能打包，可按需改老板口吻。

## 配置（`.env`，可选但推荐）

技能目录自带 `.env.example`，移植到新机：`cp .env.example .env` 后按需填写。所有项均可选，留空=自动探测/默认。已存在的系统环境变量不被 `.env` 覆盖。

- **CodeBuddy 认证**：见上方「运行」段的 ⚠️ CodeBuddy 认证（API Key 或 CLI 已登录二选一）。
- **其他可配项**：`POLL_INTERVAL`(轮询秒)、`DRY_RUN=1`(只验证不真发)、`REPLY_DELAY_SEC`(延迟窗口秒,默认120)、`ACTIVE_WINDOW_SEC`(活跃窗口秒,默认300)、`GROUP_PUSH=atme|all|off`(群消息推微信策略,默认 `atme`)、`MENTION_NAMES`(群 @我 昵称候选,在私密 `.env` 配你的昵称)、`SKIP_SENDERS`(不代发名单)、`SELF_SENDERS`(本人昵称)、`AUTO_REPLY_IMAGE`(单聊图片代复开关,默认开)、`VISION_API_KEY/BASE_URL/MODEL`(图片识别后端,默认 hy3 零配置)、各二进制路径。全在 `.env.example` 有注释。
