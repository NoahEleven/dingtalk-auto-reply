#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""reply.py —— 回复生成层（CodeBuddy Agent SDK 唯一后端，无 CLI 兜底）。

包含：人设加载、多维表事实 grounding（意图识别 + 查表 + 缓存）、
_gen_reply_sdk_async（SDK 异步生成）、gen_reply（带安全网）、
extract_reply / _looks_like_reply（质量净化）、push_weixin（微信推送）、_seed_notify。

共享常量与运行期辅助来自 runtime.py；dws 调用来自 dingtalk_api.py；
图片视觉类型来自 vision.py。DRY_RUN 等可变配置走 runtime 属性访问。
"""
import os, json, time, re, asyncio, subprocess
import runtime
from runtime import (
    _SDK_AVAILABLE, _sdk_query, CodeBuddyAgentOptions, AppendSystemPrompt,
    AssistantMessage, TextBlock, ResultMessage,
    ThinkingBlock, ToolUseBlock, ToolResultBlock,
    BOSS_UID, BOSS_NAME, BOSS_COMPANY, BOSS_TITLE, TABLE_GROUNDING, TEST_MODE,
    GBRAIN_GROUNDING, GBRAIN_MCP_URL, GBRAIN_MCP_TOKEN,
    FB_BASE, FB_TAB, FB_STATUS, FB_HANDLER, FB_SUMM,
    ROS_BASE, ROS_TAB, ROS_PROG, ROS_OWNER, ROS_NAME,
    SEND_JS, NODE, CHINA_EDITION, CODEBUDDY_API_KEY, CODEBUDDY_MODEL,
    CODEBUDDY_CMD, DINGTALK_WORKSPACE, VISION_ENABLED, log_debug, log_audit,
    CREATE_NO_WINDOW, DISALLOWED_TOOLS, build_sdk_env, build_image_block,
    _vision_media_type,
)
from dingtalk_api import run_dws


# ---------- 多维表事实 grounding（查表 + 缓存 + 意图识别） ----------
# 表结构常量全部从 runtime 经环境变量注入（源码不硬编码任何 baseId/
# tableId/字段 ID，隐私安全）；未配置（FB_BASE/ROS_BASE 为空）时
# fetch_table_context 自动退化为纯人设代复，不报错、不崩。


# 缓存：意图 -> (时间戳, 文本)，避免同一会话重复拉表（省时省配额）
_TABLE_CACHE = {}
_TABLE_CACHE_TTL = 300  # 秒

# 会话记忆：sid -> 是否已在 codebuddy 侧 resume 过（同一对话多次消息共用上下文，记忆连续）
SESSION_RESUMED = {}

# 冷启动放宽标记：进程内是否成功完成过一次 SDK 生成。首次（未成功过）调用使用
# AGENT_FIRST_CALL_TIMEOUT（覆盖 codebuddy 一次性注册延迟），成功一次后切回 AGENT_CALL_TIMEOUT。
_agent_warmed = False


# ---------- 人设真源（方案 B：codebuddy 注册 agent 文件为唯一真源） ----------
# 真源 = codebuddy 全局注册 agent "dingtalk-helper"（~/.codebuddy/agents/dingtalk-helper.md），
#   由 gen_reply 经 extra_args={"agent": SOUL_AGENT} 透传 CLI --agent 按名加载
#   （已实测 18.6s 成功；首次调用有一次性冷启动注册延迟属正常，非机制故障）。
#   故 agent 已注册时 _resolve_persona() 返回 None（不重复注入，避免双灵魂）。
# 便携备份 = skill 内 dingtalk-helper-backup.md（dingtalk-helper.md 的干净模板/部署副本，随 skill 移植；
#   新机器尚未注册 agent 时自动兜底注入；本文件不含任何私人数据，仅作占位模板）。
#   旧 secretary_system_prompt.txt / reply_persona_grounded.md 已删除（内容均被 dingtalk-helper.md
#   覆盖；且前者会教模型"自行调 dws" 与 _MODE_LOCK 的"严禁自己调 dws" 直接冲突，留作兜底反而有害）。
_SKILL_DIR = os.path.dirname(os.path.abspath(__file__))
SOUL_AGENT = "dingtalk-helper"
_CODEBUDDY_AGENTS_DIR = os.path.join(os.path.expanduser("~"), ".codebuddy", "agents")
SOUL_AGENT_FILE = os.path.join(_CODEBUDDY_AGENTS_DIR, f"{SOUL_AGENT}.md")
PERSONA_BACKUP_FILE = os.path.join(_SKILL_DIR, "dingtalk-helper-backup.md")
# ⚠️ 2026-08-03 老板要求：dws 查同事记录 few-shot 示例文件（每次调用注入 system_prompt，
# 教 agent 遇到「对接/进展/记录」类问题时先调 dws 查真实记录再答，禁止偷懒凭印象）。
# 文件用中性占位词（同事甲/小王），不含任何真实人名/隐私，可随包分发。
DWS_EXAMPLES_FILE = os.path.join(_SKILL_DIR, "dws-reply-examples.md")
_AGENT_REGISTERED = os.path.isfile(SOUL_AGENT_FILE)
if _AGENT_REGISTERED:
    log_debug(f"[persona] 方案B 生效：人设真源=codebuddy agent '{SOUL_AGENT}' ({SOUL_AGENT_FILE})")
else:
    log_debug(f"[persona] agent 未注册（{SOUL_AGENT_FILE} 不存在），将兜底注入 skill 内 dingtalk-helper-backup.md")


def _read_soul(path):
    """读灵魂文件正文，剥掉 YAML frontmatter（--agent 加载才需 frontmatter；
    注入 system_prompt 时只要正文，避免 '--- name: ... ---' 变成提示词噪音）。"""
    try:
        with open(path, encoding="utf-8") as f:
            txt = f.read().strip()
    except Exception:
        return ""
    if txt.startswith("---"):
        end = txt.find("\n---", 3)
        if end != -1:
            txt = txt[end + 4:].strip()
    return txt


def _resolve_persona():
    """方案 B：人设真源解析（经 --agent 按名加载 codebuddy 注册灵魂，见文件头）。
    真源 = ~/.codebuddy/agents/dingtalk-helper.md；agent 已注册时由 CLI --agent 加载，
    _resolve_persona() 返回 None 不重复注入（避免双灵魂）。
    - agent 已注册 → 返回 None（灵魂由 --agent 加载，system_prompt 只补 _MODE_LOCK+查表）。
    - 未注册（如移植新机器）→ 退回 skill 内 dingtalk-helper-backup.md 注入。
    - 备份也缺 → 退回硬编码兜底话术（不会让程序崩）。"""
    if _AGENT_REGISTERED:
        return None
    t = _read_soul(PERSONA_BACKUP_FILE)
    if t:
        log_debug(f"[persona] agent 未注册，兜底注入：{os.path.basename(PERSONA_BACKUP_FILE)}")
        return t
    return (f"你是{BOSS_NAME}，{BOSS_COMPANY}的{BOSS_TITLE}，正在钉钉上以本人身份回同事消息。"
            "语气自然平实，像真人同事闲聊；别暴露你是AI/助理，禁止自我介绍。")


# 模式锁：gen_reply 始终以「直接回复同事模式」注入系统提示，
# 避免双模式 agent（dingtalk-helper）默认进探索模式、把同事消息当成给助理的任务、
# 反问"把进度发我"/"我直接拟回复"（已实测偶发，输出不稳定、抖动）。
# 核心：明确"你是本公司一名普通员工、对方是平级同事"，从源头掐掉"我是替老板写稿的"
# 或"我是公司领导"两种错误心智——前者会让模型自我指涉/过度谦卑，后者会命令式拍板。
# 身份全部走变量（BOSS_NAME/BOSS_COMPANY/BOSS_TITLE），源码不硬编码任何隐私。
# 当 agent 已注册时 persona=None，此处是唯一的强制锁，必须始终注入。
_MODE_LOCK = (
    f"【角色与任务】你现在就是本人【{BOSS_NAME}】，在钉钉上和一位【同事 / 平级协作者】聊天（对方不是你的下属）。"
    f"你这一轮唯一要做的事：基于下方真实上下文，写出你要发给这位同事的【回复正文】。\n"
    "【⚠️ 先查再答 · 任务前置步骤】若主消息涉及【产品规格 / 硬件参数 / 接口定义 / 配置步骤 / 说明书 / 项目进展】等"
    "事实性问题（典型信号：问\"多少 / 是什么 / 怎么配 / 默认值 / 参数\"等），"
    "你【必须先调 mcp__gbrain__search 检索知识库】拿到真实数据，再据此写回复——"
    "这是隐式检索步骤，不展示给同事；最终对外输出仍是 <reply>...</reply> 包裹的回复正文。\n"
    "  · 【禁止凭印象答事实性问题】绝不出现\"印象里 / 我记得 / 大概 / 应该是\"等含糊词回答规格参数；"
    "拿不准就先调 gbrain 查，查不到如实说\"这个我得查下资料确认，稍回你\"。\n"
    "  · 【调 gbrain 不违反'只输出回复'】调用 mcp__gbrain__search / mcp__gbrain__query 是工具调用，"
    "不会污染最终输出；模型中间步骤的 tool_use 不会展示给同事，只有 <reply>...</reply> 内的文本会发出。\n"
    "【⚠️ 查同事聊天记录 · dws 命令行】若主消息提到【具体同事的人名】并涉及\"对接 / 进展 / 之前说过 / 谁负责 / 记录\""
    "等需要翻聊天记录才能回答的问题（典型信号：\"对接 XX 了吗 / 跟 XX 说了吗 / XX 有反馈吗\"），"
    "你【必须用 Bash 调 dws 命令行查该同事的聊天记录】拿到真实情况再回，不要凭空说\"我还没对接\"：\n"
    "  · 先查该同事的 userId：`dws contact user search --query \"同事姓名\" --format json`（取 result[].userId）；\n"
    "  · 再拉 TA 最近发来的消息：`dws chat message list-by-sender --sender-user-id <userId> --start \"YYYY-MM-DDT00:00:00+08:00\" --end \"YYYY-MM-DDT23:59:59+08:00\" --limit 20 --format json`"
    "（时间窗按近 1~2 个月填，看 result.conversationMessagesList[].messages[].content / createTime）；\n"
    "  · 查到了就基于真实记录回答——直接说事实（时间点 / TA 原话 / 谁在做），"
    "绝不在回复里暴露'我查了记录'（真人不会说'我查了下聊天记录'，直接说'跟进了，X月X号跟 TA 聊过…'即可）；"
    "命令失败 / 查不到则如实说\"这个我得查下确认，稍回你\"，绝不编造\"已对接 / 没对接\"。"
    "dws 是命令行工具，Bash 调用与手动敲命令等价，属隐式检索步骤，不违反\"只输出回复\"。\n"
    "【只写不干】不要进入探索模式、不要给选项——可就关键细节反问同事确认，最终只输出回复本身。\n"
    "【身份红线】你是公司普通员工，不是领导：涉及花钱 / 采购 / 签约 / 录用 / 排期 / 金额等需本人拍板的事，"
    "不要擅自答应，改说\"这个我确认下再回你\"。绝不暴露你是 AI / 助理 / 机器人，绝不称呼对方为\"老板\""
    "（那是助理对你的昵称）；不主动向同事透露真实姓名 / 公司 / 城市 / 电话 / 邮箱等隐私，对方问起也含糊带过。\n"
    "【语气 · 像老板真人，不是 AI 汇报】自然平实、1-3 句，像平时随手打字：直接说结论/事实，不解释过程、"
    "不暴露查询动作（不说\"我查了记录/查询结果显示/根据聊天记录\"）；有判断有态度（该催就催\"我催下他\"、"
    "该等就等\"他弄完我同步你\"）；别官腔、别客服腔（\"好的呢~\"\"收到亲\"\"您放心\"全禁）、别摆架子；"
    "技术话题自然中英混排。\n"
    "【话题纪律】每次只针对【主回复对象】那一条消息回复。背景历史仅供理解上下文与模仿你的口吻，"
    "【绝不】把历史里已回复过的话题、已被处理过的事（如\"问题反馈表 / 项目进度\"跟进）主动带回当前回复；"
    "若主消息与历史无关，则完全忽略历史。\n"
    "【图片】若主消息附图，直接结合图片内容以本人口吻回应、第一句进主题；绝不说\"我看一下图 / 下载下来看\"。\n"
    "只把回复正文用 <reply></reply> 包裹后输出，标签外不写任何字。\n"
)


def build_knowledge_instruction():
    """知识库检索指令 —— 与 gbrain 实际挂载状态保持一致（避免「prompt 让调不存在的工具」悬空指令）。

    关键设计（老板纠偏）：gbrain 是**检索工具、按需调用**，不是每条消息都查；纯闲聊 /
    问候 / 安排 / 表态等无需事实依据的，直接基于对话上下文作答，绝不为查而查。

    挂载状态由 runtime.GBRAIN_GROUNDING / runtime.GBRAIN_MCP_URL 实时决定（与
    _gen_reply_sdk_async 里 mcp_servers 的挂载判断用**同一来源**），保证：
      - gbrain 已挂载  → 告知「按需调 gbrain，失败/无结果回退本地文件」；
      - gbrain 未挂载  → 明确「gbrain 未启用、不要调它」，直接走本地文件（否则会悬空指令）。
    """
    _gb_on = bool(runtime.GBRAIN_GROUNDING and runtime.GBRAIN_MCP_URL)
    if _gb_on:
        return (
            "【参考资料 · 知识库检索：gbrain 为唯一默认入口，本地文件仅作保底】\n"
            "  · 【事实性问题必须先查 gbrain · 不是按需是必须】你的知识库已接入 gbrain（MCP 服务，工具：mcp__gbrain__search / mcp__gbrain__query）。\n"
            "    凡是回复涉及【产品规格 / 硬件参数 / 接口定义(485/波特率/使能/从机地址) / 配置步骤 / 说明书 / 项目进展 / 本人记忆背景】\n"
            "    等需要事实依据的内容，【必须先调 gbrain 检索】拿到真实数据再答——这不是\"按需\"，是\"必须\"。\n"
            "    典型触发信号：对方问\"多少 / 是什么 / 怎么配 / 默认值 / 参数 / 哪个型号 / 进度\"等。\n"
            "    这些资料（含各型号产品手册、项目资料、本人记忆）【都已收录进 gbrain】，不要再去翻本地或外部 PDF。\n"
            "  · 【gbrain 怎么用】\n"
            "      - mcp__gbrain__search(query=\"<关键词>\", limit=5)：关键词/全文检索，快速定位相关片段；\n"
            "      - mcp__gbrain__query(query=\"<问题>\")：向量+关键词混合检索，适合问答式查找。\n"
            "  · 【纯闲聊才不查】只有纯问候 / 安排 / 表态 / 调侃 / 确认收到 等完全无需事实依据的消息，才直接基于对话上下文作答，不调 gbrain；\n"
            "    事实性问题绝不允许凭记忆硬答\"印象里 / 我记得 / 大概 / 应该是\"——这种含糊词出现即视为违规。\n"
            "  · 【严禁用 WebSearch / WebFetch 查内部资料】上述产品 / 硬件 / 接口 / 配置 / 说明书 / 项目 / 本人记忆类内容，\n"
            "    **严禁用 WebSearch / WebFetch 搜公网**——内部资料本就不在公网、搜不到还易泄露。WebSearch / WebFetch 仅限：\n"
            "    gbrain 与本地都查不到、且确属公开通用信息（如某通用协议公开标准）时。\n"
            "  · 【调用顺序铁律】在调用 Read / Grep / Glob / Bash 去读本地文档之前，你必须先调一次 gbrain；本地读仅在 gbrain 失败后才被允许。\n"
            "  · 【本地文件：仅当 gbrain 真的失败才用】若 gbrain 调用失败 / 超时 / 检索结果为空或明显不对题，\n"
            "    才回退到工作空间本地文档核实：`产品资料/`、`项目文件/`、`.workbuddy/memory/*.md`（仅理解上下文，不复制进回复）。\n"
            "    注意：本地核实是【兜底、不是首选】——gbrain 收录更全更准，能 gbrain 就别逐文件翻阅本地（慢且易遗漏）。\n"
            "  · 只陈述检索 / 核实到的真实内容；源自 gbrain 写「参考：gbrain 知识库 · <标题>」，源自本地写「参考文件：<文件名>」（只写名不写路径）。\n"
            "    两路都查不到才如实说\"这个我得查下资料确认，稍回你\"，绝不硬编。\n"
            "【严禁编造】绝不凭空编造规格 / 参数 / 配置步骤 / 文件名，也不引用不存在的内容（无论来自 gbrain 还是本地文件）。\n"
        )
    # gbrain 未挂载：明确不要去调 gbrain，直接走本地文件，杜绝悬空指令
    return (
        "【参考资料 · 知识库检索：本地文件（gbrain 未启用）】\n"
        "  · 当前 gbrain 知识库未接入（GBRAIN_GROUNDING=0 或端点未配置），**不要**尝试调用 gbrain 相关工具。\n"
        "  · 需要事实依据时，直接用 Grep/Glob 定位、Read 核实工作空间本地文档：\n"
        "      - `产品资料/`：产品规格、接口定义、配置手册、说明书；\n"
        "      - `项目文件/`：研发项目库索引 → 项目进展 / 任务归属；\n"
        "      - `.workbuddy/memory/*.md`：项目背景、同事关系、本人说话习惯，仅用于理解上下文、不复制进回复。\n"
        "  · 只陈述核实到的真实内容；回复产品 / 资料类问题时正文末尾加「参考文件：<文件名>」（只写文件名、不写路径）。"
        "查不到才如实说\"这个我得查下资料确认，稍回你\"，绝不硬编。\n"
        "【严禁编造】绝不凭空编造规格 / 参数 / 配置步骤 / 文件名。\n"
    )

def detect_table_intent(content):
    """根据消息内容判断要查哪张表：'feedback'=问题反馈表，'project'=ROS项目表，None=不查。"""
    if not content:
        return None
    t = content
    # 问题反馈表意图词
    if any(k in t for k in ("问题反馈", "反馈进度", "问题进度", "待解决", "反馈", "需求汇总", "问题更新")):
        return "feedback"
    # ROS 项目表意图词
    if any(k in t for k in ("项目进度", "项目表", "ROS", "在跑", "研发项目", "项目做到", "进度怎么样", "做到哪")):
        return "project"
    return None


def _clean_md(s):
    """清洗注入数据里的图片/链接 markdown，避免坏语法（截断的 URL）干扰模型。"""
    s = re.sub(r"!\[[^\]]*\]\([^)]*\)", " [图片] ", s)  # 图片 -> [图片]
    s = re.sub(r"\[[^\]]*\]\([^)]*\)", "", s)            # 其余链接去掉
    return s


def _format_feedback(records):
    items = []
    for r in records:
        c = r.get("cells", {})
        st = c.get(FB_STATUS, {})
        st = st.get("name") if isinstance(st, dict) else st
        summ = c.get(FB_SUMM, {})
        summ = summ.get("markdown", "") if isinstance(summ, dict) else str(summ)
        summ = _clean_md(summ)
        summ = re.sub(r"\s+", " ", summ).strip()
        if len(summ) > 160:
            summ = summ[:160] + "…"
        items.append(f"  · [{st}] {summ}")
    return (
        "【问题反馈表·真实数据（来源：问题实时反馈与更新需求汇总）】\n"
        f"筛选条件：处理人包含{BOSS_NAME}(userId={BOSS_UID}) 且 解决状态≠已解决\n"
        f"当前{BOSS_NAME}名下共 {len(items)} 条待解决：\n" + "\n".join(items) +
        "\n（注：这是「问题反馈表」，与「ROS软件项目管理表/项目表」是两回事，回复时请明确区分，不要混淆。）"
    )


def _format_project(records):
    items = []
    for r in records:
        c = r.get("cells", {})
        name = c.get(ROS_NAME, "")
        prog = c.get(ROS_PROG)
        try:
            prog_pct = f"{int(float(prog)*100)}%"
        except Exception:
            prog_pct = str(prog)
        items.append(f"  · [{prog_pct}] {name}")
    return (
        "【ROS软件项目管理表·真实数据（来源：ROS软件项目管理表）】\n"
        f"筛选条件：负责人包含{BOSS_NAME}(userId={BOSS_UID}) 且 进度≠100%\n"
        f"当前{BOSS_NAME}名下共 {len(items)} 个在跑项目：\n" + "\n".join(items) +
        "\n（注：这是「ROS项目表」，与「问题实时反馈与更新需求汇总/问题反馈表」是两回事，请明确区分。）"
    )


def fetch_table_context(intent, session_id=None):
    """按意图拉取对应多维表的「本人名下未解决项」，返回格式化文本（带缓存）。
    返回空串表示拉取失败、未配置多维表、或无需查表。"""
    if intent not in ("feedback", "project"):
        return ""
    # 未配置多维表（FB_*/ROS_* 为空）→ 自动退化为纯人设代复，不查表、不报错。
    if intent == "feedback" and not (FB_BASE and FB_TAB and BOSS_UID):
        return ""
    if intent == "project" and not (ROS_BASE and ROS_TAB and BOSS_UID):
        return ""
    now = time.time()
    cached = _TABLE_CACHE.get(intent)
    if cached and (now - cached[0]) < _TABLE_CACHE_TTL:
        log_debug(f"[table-cache] hit intent={intent}")
        return cached[1]
    try:
        if intent == "feedback":
            filt = json.dumps({"operator": "and",
                               "operands": [{"operator": "ne",
                                             "operands": [FB_STATUS, "已解决"]}]},
                              ensure_ascii=False)
            out = run_dws(["aitable", "record", "query", "--base-id", FB_BASE,
                           "--table-id", FB_TAB, "--filters", filt, "--all", "--format", "json"],
                          timeout=60)
            data = json.loads(out)
            recs = data.get("data", {}).get("records", [])
            boss = [r for r in recs if BOSS_UID in [
                x.get("userId") for x in (r.get("cells", {}).get(FB_HANDLER) or [])
                if isinstance(x, dict)]]
            text = _format_feedback(boss)
        else:
            out = run_dws(["aitable", "record", "query", "--base-id", ROS_BASE,
                           "--table-id", ROS_TAB, "--all", "--format", "json"], timeout=60)
            data = json.loads(out)
            recs = data.get("data", {}).get("records", [])
            boss = []
            for r in recs:
                c = r.get("cells", {})
                uids = [x.get("userId") for x in (c.get(ROS_OWNER) or []) if isinstance(x, dict)]
                try:
                    prog = float(c.get(ROS_PROG) or 0)
                except Exception:
                    prog = 0.0
                if BOSS_UID in uids and prog < 1.0:
                    boss.append(r)
            text = _format_project(boss)
        _TABLE_CACHE[intent] = (now, text)
        log_debug(f"[table] intent={intent} pulled, boss_items={len(boss)}")
        return text
    except Exception as e:
        log_debug(f"[table] fetch failed intent={intent}: {e}")
        return ""


async def _gen_reply_sdk_async(persona, prompt, image_paths=None,
                                session_id=None, resume=False):
    """用 CodeBuddy Agent SDK 异步生成回复（主后端）。
    编程式 system_prompt（无命令行编码/换行坑）、SDK 自管 stdio（无管道挂死）。
    返回原始回复文本（未走安全网净化）。
    坑：SERVER__PORT 端口冲突会导致 query() 0 字节超时 → 用空闲端口注入 env 规避。
    image_paths 非空时，把图以 Anthropic image 协议内联进用户消息（deepseek-v4-flash 多模态），
    实现「看图代复一次调用」——无需先 describe 再代复。
    session_id/resume：按会话隔离 + 续上下文（dt_<cid> 一人一会话，记忆连续）。
    cwd 固定钉钉工作空间 → 自动加载该空间记忆(.workbuddy/memory/MEMORY.md)；
    system_prompt 用 AppendSystemPrompt（追加模式），保留被自动加载的工作空间记忆，不被整体覆盖。"""
    sdk_env = build_sdk_env()
    # gbrain 知识库 MCP（HTTP）：让回复 agent 直接用 search/query 做语义检索，
    # 取代「工作空间内 产品资料/项目文件 文档」的旧 Grep/Glob/Read 注入方式。
    # 以 HTTP 客户端连接已运行的 gbrain（单一进程持 PGLite 锁），不 spawn 新进程、不抢锁。
    # GBRAIN_GROUNDING=0 或端点不可用时不挂（退回纯人设代复 + 多维表 grounding）。
    mcp_servers = {}
    # 与 build_knowledge_instruction() 同读 runtime 实时值 → prompt 指令与工具挂载永不一致
    if runtime.GBRAIN_GROUNDING and runtime.GBRAIN_MCP_URL:
        _gb_hdr = {"Authorization": f"Bearer {runtime.GBRAIN_MCP_TOKEN}"} if runtime.GBRAIN_MCP_TOKEN else {}
        mcp_servers["gbrain"] = {
            "type": "http",
            "url": runtime.GBRAIN_MCP_URL,
            "headers": _gb_hdr,
        }
    options = CodeBuddyAgentOptions(
        # 方案 B：agent 已注册时由 --agent 按名加载灵魂，不再注入人设（避免双灵魂）；
        # 仅 agent 未注册时把 sys_text（人设兜底+查表数据）以追加模式注入。
        system_prompt=(
            AppendSystemPrompt(append=persona) if (AppendSystemPrompt and persona) else None
        ),
        # 真源：透传 --agent 让 codebuddy 按名加载全局注册灵魂 dingtalk-helper.md（已实测 18.6s 成功）；
        # agent 未注册（如新机器）时不传，退回 sys_text 文件注入兜底。
        extra_args={"agent": SOUL_AGENT} if _AGENT_REGISTERED else {},
        # 工具权限统一管理（黑名单模式）：禁用 DISALLOWED_TOOLS 列出的工具（详见 runtime.py）。
        # 默认只禁 Write/Edit（防 agent 误写/篡改本地文件）；WebSearch/Read/Grep 等放开，
        # 但 system_prompt 仍软约束「事实优先查 gbrain、内部资料不丢公网」。gbrain/dws 均为 MCP 工具，不受此禁影响。
        # 如需调整，改 runtime.py 的 DISALLOWED_TOOLS 或用环境变量 DINGTALK_AGENT_DISALLOWED_TOOLS 覆盖。
        disallowed_tools=DISALLOWED_TOOLS,
        model=CODEBUDDY_MODEL,
        permission_mode="bypassPermissions",
        codebuddy_code_path=CODEBUDDY_CMD if CODEBUDDY_CMD and os.path.exists(CODEBUDDY_CMD) else None,
        cwd=DINGTALK_WORKSPACE if os.path.isdir(DINGTALK_WORKSPACE) else None,
        env=sdk_env,
        mcp_servers=mcp_servers,
        session_id=session_id if (session_id and not resume) else None,
        resume=session_id if (session_id and resume) else None,
    )
    async def _prompt_iter():
        if image_paths:
            content = [{"type": "text", "text": prompt}]
            for p in image_paths:
                try:
                    content.append(build_image_block(p))
                except Exception as e:
                    log_debug(f"[gen_reply] read image {p} error: {e}")
            yield {"type": "user", "message": {"role": "user", "content": content}}
        else:
            yield {"type": "user", "message": {"role": "user", "content": prompt}}
    chunks = []
    # ⚠️ 2026-08-03 老板要求：agent 是流式输出，可观察完整思考/执行过程。
    # 解析时除 TextBlock（最终回复）外，把 ThinkingBlock（思考）/ ToolUseBlock（工具名+入参）/
    # ToolResultBlock（工具结果）也打进调试日志——排查「agent 到底调没调 dws / 查到了什么」时
    # 直接看日志轨迹，不用猜。默认关（DEBUG_AGENT_TRACE=1 开启），避免常驻下刷屏。
    _trace = os.environ.get("DEBUG_AGENT_TRACE") == "1"
    _trace_buf = []
    async for message in _sdk_query(prompt=_prompt_iter(), options=options):
        if isinstance(message, AssistantMessage):
            for block in message.content:
                if isinstance(block, TextBlock):
                    chunks.append(block.text)
                elif _trace:
                    if isinstance(block, ThinkingBlock):
                        _trace_buf.append(f"[thinking] {block.thinking[:300]}")
                    elif isinstance(block, ToolUseBlock):
                        _trace_buf.append(f"[tool_use] {block.name} input={json.dumps(block.input, ensure_ascii=False)[:400]}")
                    elif isinstance(block, ToolResultBlock):
                        _trace_buf.append(f"[tool_result] {str(block.content or '')[:400]} is_error={block.is_error}")
        elif _trace and isinstance(message, ResultMessage):
            _trace_buf.append(f"[result] {str(getattr(message, 'result', ''))[:300]}")
    if _trace and _trace_buf:
        log_debug("[agent-trace]\n" + "\n".join(_trace_buf))
    return "".join(chunks).strip()


def _build_user_msg(main_line, history_lines, burst_mode):
    """构造 gen_reply 的「用户消息」正文（主回复对象 + 可选背景历史）。

    把原先 4 套 80% 重合的模板（burst/single × 有/无背景历史）收敛为一张查表，
    按 (burst_mode, 是否有背景历史) 取对应的「主回复对象描述句 / 背景历史块开头 / 结尾指令」
    三段差异化片段，再统一拼装——消除 4 套重复 inline 模板，且输出与原模板逐字节一致。
      - main_line:      "发送者：内容" 形式的主回复对象行
      - history_lines:  背景历史逐行列表（已标注"你(本人历史发言)"/同事）；空则无背景块
      - burst_mode:     True=主消息是延时期连发的多条，要求综合给一条统一回复
    """
    _main_head, _hist_head, _tail = _USER_MSG_FRAGMENTS[(burst_mode, bool(history_lines))]
    parts = [
        "【⚠️ 你的任务关键 ⚠️】\n",
        f"● 【主回复对象】（{_main_head}）\n",
        f"  {main_line}\n\n",
    ]
    if history_lines:
        parts.append(
            f"● 【背景历史】（更早的消息，{_hist_head}；仅供了解上下文和你的说话风格，【不是】回复对象）\n"
            + "\n".join(history_lines) + "\n\n"
            "● SDK 会话历史（如有）：仅用于了解你平时说话风格，【不是】当前要回的内容。\n\n"
        )
    else:
        parts.append("● SDK 会话历史（如有）：仅用于了解你平时说话风格。\n")
    parts.append(_tail)
    return "".join(parts)


# gen_reply 用户消息 4 种形态的差异化片段（键 = (burst_mode, 有背景历史)）。
# 主回复对象描述句 / 背景历史块开头 / 结尾指令——三段组合即原 4 套模板的全部差异，
# 提取到此表后 gen_reply 不再内联重复 f-string，且可保证 prompt 文本零漂移。
_USER_MSG_FRAGMENTS = {
    (True, True): (
        "对方在延时窗口内连发的多条消息，已按时间正序合并如下，是你【唯一】要回的内容——"
        "请综合理解后给【一条】统一回复，覆盖对方连发消息里的核心诉求，不要漏答",
        "含你本人历史发言 + 跨窗口的对方更早消息",
        "请针对【主回复对象】（即对方连发的全部消息）写一条回复：综合理解对方这一轮在表达什么，"
        "给一条涵盖核心诉求的统一回复；不要逐条复述对方的话、不要分点作答；"
        "不要复述或扩展背景历史里已说过的话题，不要再提已被处理过的\"问题反馈表 / 项目进度\"等。",
    ),
    (True, False): (
        "对方在延时窗口内连发的多条消息，已按时间正序合并如下，是你【唯一】要回的内容——"
        "请综合理解后给【一条】统一回复",
        None,
        "请综合理解对方这一轮连发的全部消息后写一条回复，覆盖对方核心诉求，不要逐条复述。",
    ),
    (False, True): (
        "对方最新发的消息，是你【唯一】要回的内容",
        "含你本人历史发言 + 对方历史消息",
        "请只针对【主回复对象】写一条回复：不要复述或扩展历史里已说过的话题，"
        "不要再提已被处理过的\"问题反馈表 / 项目进度\"等；本人历史发言仅供你模仿口吻。",
    ),
    (False, False): (
        "对方最新发的消息",
        None,
        "请以你自己的口吻写一条回复（若与历史话题无关，请完全忽略历史）。",
    ),
}


def gen_reply(sender, content, image_desc="", image_paths=None, return_rejected=False,
              session_id=None, resume=False, table_context="", messages=None):
    """生成本人身份回复（**CodeBuddy Agent SDK 后端，唯一方案，无 CLI 兜底**）。
    关键设计：**系统提示(人设+查表数据+指令) 与 用户消息(纯消息) 分离**——
    SDK 用 AppendSystemPrompt(追加模式) 承载人设与查表事实（保留 cwd 自动加载的工作空间记忆），
    用户消息只传干净单行，避免模型把"指令/数据"误当成"要回复的消息"而说"没收到内容"。
    安全网（extract_reply + _looks_like_reply）在此做：质量不达标返回空，调用方走「不代发」。
    session_id 非空时按会话隔离记忆；resume=True 表示续上同一会话（记忆连续）。

    ⚠️【主消息 vs 背景历史 · 关键设计】（2026-07-20 老板纠偏 + 修复）：
    messages 非空 = 窗口内多条消息合并：
      - msgs[0] = 对方最新发的消息 = 【主回复对象】（这是 AI 唯一要回的内容）
      - msgs[1:] = 同一对话窗口内的更早消息 = 【背景历史】（仅供 AI 了解背景）
    旧版本把所有历史"；".join 成 content 喂给 AI + 让 AI"综合理解"，导致 AI 复述最旧话题、
    配合 SDK session resume 加载的旧回复形成"话题死循环"（典型症状：AI 还在接茬 T1
    "问题反馈表，内容跟进一下"，而不是回最新一条"我以为让我测试呢"）。根治：
    (1) 主消息/历史显式分离；(2) prompt 强约束"只回主消息"；(3) 查表 grounding 用主消息判。

    SDK 不可用/失败 → 直接返回空（不代发），不再降级 CLI。
    """
    if not _SDK_AVAILABLE:
        log_debug("[gen_reply] SDK 不可用 -> 不代发")
        return ("", "") if return_rejected else ""
    persona = _resolve_persona()
    has_image = bool(image_paths)

    # —— 决定主回复对象 + 背景历史 ——
    # ⚠️ 2026-07-30 老板纠偏：延时期内（≤REPLY_DELAY_SEC）对方连发的多条消息，
    # 物理合并成【一条主消息】喂给 AI（不再走"只摘最新一条 + 其余当背景历史"的旧逻辑）。
    # 物理合并比 prompt 软约束强：AI 看到的主回复对象直接就是"对方在延时窗口内连发的全部消息"。
    # 跨窗口更早的对方消息 + 老板本人历史发言 仍作为背景历史（仅作上下文）。
    main_msg = None
    history_msgs = []
    main_content = content  # 单条分支用
    _burst_mode = False  # 是否走了"延时期合并"路径（日志区分用）
    if messages:
        # 防御再排一次（保证 newest-first）：主循环已按 ts desc 排过，但若外部
        # 直接调用 gen_reply 传入乱序 messages，AI 仍可能接错话。
        try:
            _ms = sorted(messages, key=lambda m: m.get("ts") or 0, reverse=True)
        except Exception:
            _ms = list(messages)
        # 对方消息（过滤 is_self=True），按 ts 倒序（newest-first）
        _peer_ms = [m for m in _ms if not m.get("is_self")]
        _self_ms = [m for m in _ms if m.get("is_self")]
        # 锚点：最新一条对方消息的 ts
        _T0 = (_peer_ms[0].get("ts") or 0) if _peer_ms else 0
        # 延时窗口 = [T0 - REPLY_DELAY_SEC, T0]
        _WIN = runtime.REPLY_DELAY_SEC
        _window_peer = [m for m in _peer_ms
                        if _T0 - (m.get("ts") or 0) <= _WIN]  # 含 T0 那条
        _window_peer.sort(key=lambda m: m.get("ts") or 0)  # 窗口内按时间正序呈现（旧→新）
        if len(_window_peer) >= 2:
            # 【延时期合并模式】把窗口内多条对方消息物理合并成一条虚拟主消息
            _burst_mode = True
            _latest = _window_peer[-1]  # 最新一条，拿 sender / ts / cid
            _lines = []
            for i, m in enumerate(_window_peer, 1):
                _lines.append(f"[{i}/{len(_window_peer)}] {m.get('sender','对方')}：{m.get('content','')}")
            _merged_content = "\n".join(_lines)
            main_msg = {
                "sender": _latest.get("sender", sender or "对方"),
                "content": _merged_content,
                "ts": _latest.get("ts"),
                # 注意：合成对象没有 image_paths/cid 等字段；图片走 has_image 分支另传
            }
            main_content = _merged_content
            # 背景历史 = 窗口外的对方消息 + 老板本人历史发言
            _window_ids = set(id(m) for m in _window_peer)
            history_msgs = [m for m in _ms if id(m) not in _window_ids]
            if history_msgs:
                history_msgs.sort(key=lambda m: m.get("ts") or 0)
        else:
            # 【单条主消息模式】沿用 2026-07-20 设计：主=最新对方一条，其余为背景历史
            main_msg = _peer_ms[0] if _peer_ms else (_ms[0] if _ms else None)
            history_msgs = [m for m in _ms if m is not main_msg] if main_msg else []
            if history_msgs:
                history_msgs.sort(key=lambda m: m.get("ts") or 0)
            if main_msg:
                main_content = main_msg.get("content", "") or content
        # 查表意图：用主消息判（合并模式下含全部窗口消息，单条模式下含最新一条）
        if not table_context and TABLE_GROUNDING and main_msg:
            _intent = detect_table_intent(main_content)
            if _intent:
                table_context = fetch_table_context(_intent, session_id) or ""
            _intent_dbg = _intent
        else:
            _intent_dbg = "(skip: no main_msg or table_context already set)"
        _n_self_hist = sum(1 for m in history_msgs if m.get("is_self"))
        _mode_tag = "burst-merged" if _burst_mode else "single-main"
        log_debug(
            f"[gen_reply] messages={len(_ms)} (peer={len(_peer_ms)} self_hist={_n_self_hist}) "
            f"mode={_mode_tag} window_peer={len(_window_peer) if _burst_mode else 1} "
            f"main_sender={main_msg.get('sender', '?') if main_msg else '?'} "
            f"main_content={(main_content[:80] if main_content else '')!r} "
            f"history_n={len(history_msgs)} intent={_intent_dbg} "
            f"table_context_len={len(table_context) if table_context else 0}"
        )
    else:
        # 单条 content 分支：兼容老调用（外部传 content 但无 messages）
        if not table_context and TABLE_GROUNDING and content:
            _intent = detect_table_intent(content)
            if _intent:
                table_context = fetch_table_context(_intent, session_id) or ""
            log_debug(f"[gen_reply] single-mode intent={_intent} table_context_len={len(table_context) if table_context else 0}")
        else:
            log_debug(f"[gen_reply] single-mode (no table) content={content[:60]!r}")

    # —— 系统提示 = (人设兜底[仅 agent 未注册时由 _resolve_persona 返回]) + (直接回复同事模式锁) + (查表数据，按主消息收紧) ——
    # 注：agent 已注册时 persona=None（灵魂由 --agent 按名加载），此处只补 _MODE_LOCK+查表；
    # _MODE_LOCK 强制「直接回复同事」模式（防双模式灵魂误进探索模式），无论 agent 是否注册都始终注入。
    parts = []
    if persona:
        parts.append(persona)
    parts.append(_MODE_LOCK)
    # ⚠️ 2026-08-03 老板要求：dws 查同事记录 few-shot 示例（教 agent 先查再答，禁止偷懒）。
    # 每次调用都注入，避免模型靠 prompt 软约束「选择性执行」（实测第一次会调 dws、第二次偷懒不调）。
    _few = _read_soul(DWS_EXAMPLES_FILE)
    if _few:
        parts.append(_few)
    parts.append(build_knowledge_instruction())  # 知识库检索指令：与 gbrain 实际挂载状态一致
    if table_context:
        # ⚠️ 收紧：再校验一次主消息是否在问「进度/反馈」类问题。
        # 旧版本无脑注入"对方问的正是上面这类进度/反馈问题"会强制 AI 聊历史，
        # 即使新消息是"测试/忙/吐槽/确认"等完全无关话题。核心 bug 根因之一。
        _main_for_intent = (main_msg.get("content", "") if main_msg else (content or ""))
        _recheck_intent = detect_table_intent(_main_for_intent)
        if _recheck_intent:
            parts.append(
                f"【多维表真实数据（已为你查好，必须基于它作答，不要编造、不要说"
                f"\"在的/收到了/好的\"之类废话）】\n{table_context}\n"
                f"对方（{sender}）最新一条消息确实在问「进度/反馈」相关问题，"
                f"请基于上方真实数据，以你自己的口吻直接告诉这位同事："
                f"当前你名下有几条待解决、分别是什么（一句话点每条）。"
            )
        else:
            # 主消息与查表意图无关 → 丢弃查表数据，避免污染当前回复。
            log_debug(f"[gen_reply] table_context dropped: main msg has no progress/feedback intent")
    sys_text = "\n\n".join(parts)

    # —— 用户消息 = 【主回复对象】+ 【背景历史】（仅作上下文） ——
    if main_msg is not None:
        _main_line = f"{main_msg.get('sender', sender or '对方')}：{main_msg.get('content', '')}"
        _hist_lines = []
        for m in history_msgs:
            if m.get("is_self"):
                _who = "你（本人历史发言）"
            else:
                _who = m.get("sender", "同事")
            _hist_lines.append(f"  · {_who}：{m.get('content', '')}")
        user_msg = _build_user_msg(_main_line, _hist_lines, _burst_mode)
        if has_image and not image_desc:
            user_msg += "\n（主消息附了一张图片，请结合图片内容以你自己的口吻回复）"
        elif image_desc:
            user_msg += f"\n（图片内容：{image_desc}）"
    else:
        # 单条 content（无 messages）：沿用旧逻辑，但 prompt 强化
        _single = f"{sender}：{content}".replace("\n", " ")
        user_msg = (
            "【⚠️ 你的任务关键 ⚠️】这是对方最新发来的【单条】消息，请直接以本人身份回复：\n"
            + _single
        )
        if has_image and not image_desc:
            user_msg += "（随消息附了一张图片，请结合图片内容以你自己的口吻回复）"
        elif image_desc:
            user_msg += f"（图片内容：{image_desc}）"
    global _agent_warmed
    _call_timeout = runtime.AGENT_FIRST_CALL_TIMEOUT if not _agent_warmed else runtime.AGENT_CALL_TIMEOUT
    try:
        raw = asyncio.run(asyncio.wait_for(
            _gen_reply_sdk_async(sys_text, user_msg, image_paths=image_paths,
                                 session_id=session_id, resume=resume),
            timeout=_call_timeout))
        # ⚠️ 2026-08-03 老板拍板：SDK 空返回自动重试 1 次。
        # 空返回 = SDK 调用"成功"但模型侧没产出任何 TextBlock（后端抖动 / 只输出工具调用没给最终文本 /
        # gbrain 链路瞬时中断），实测复现时 16s 快速返回空、rejected_len=0。属偶发，重试大概率成功。
        # 重试时在用户消息尾部追加一句提示，要求直接输出 <reply> 正文，避免再次只给分析/工具调用过程。
        if not raw:
            log_debug("[gen_reply] SDK 空返回 -> 自动重试 1 次")
            raw = asyncio.run(asyncio.wait_for(
                _gen_reply_sdk_async(
                    sys_text,
                    user_msg + "\n（注意：上一次生成未返回有效内容，请直接输出 <reply>...</reply> 包裹的回复正文；不要只输出分析过程或工具调用过程。）",
                    image_paths=image_paths, session_id=session_id, resume=resume),
                timeout=_call_timeout))
            if raw:
                log_debug(f"[gen_reply] 重试成功，len={len(raw)}")
            else:
                log_debug("[gen_reply] 重试仍空返回 -> 转人工")
    except Exception as e:
        if isinstance(e, asyncio.TimeoutError) and not _agent_warmed:
            # 首次调用超时：几乎都是 codebuddy 仍在冷启动注册 agent（7~26min），并非真故障。
            # 本次不代发（转人工），但保留 _agent_warmed=False，进程内后续调用仍用大超时，
            # 直到注册完成成功一次才切回常规超时——根治「首条消息被 240s 静默跳过」。
            log_debug(
                f"[gen_reply] 首次 SDK 调用在 {_call_timeout}s 内超时——大概率 codebuddy 仍在冷启动注册 agent，"
                f"本次不代发（转人工通知）；进程内后续调用已自动保持放宽超时，注册完成后即可正常代复"
            )
            return ("", "[SDK调用超时] codebuddy 可能仍在冷启动注册 agent") if return_rejected else ""
        # SDK 调用抛其他异常（如 400 invalid parameter value）：把异常详情+响应体打到日志，
        # 并把失败原因回传给老板，避免只笼统说「质量不达标」却不知根因。
        _resp = getattr(e, "response", None)
        _resp_txt = ""
        if _resp is not None:
            try:
                _resp_txt = _resp.text if hasattr(_resp, "text") else str(_resp)
            except Exception:
                _resp_txt = ""
        log_debug(f"[gen_reply] SDK 调用失败 -> 不代发: {type(e).__name__}: {e}")
        if _resp_txt:
            log_debug(f"[gen_reply] SDK 响应体(前500): {_resp_txt[:500]}")
        return ("", f"[SDK调用失败] {type(e).__name__}: {e}") if return_rejected else ""
    _agent_warmed = True  # 成功完成一次 SDK 生成，后续调用用常规超时
    if not raw:
        return ("", "") if return_rejected else ""
    out = extract_reply(raw)
    if not _looks_like_reply(out):
        log_debug(f"gen_reply sanity fail (len={len(out)}) -> 不代发")
        log_debug(f"[gen_reply] 质检未过草稿预览(前600): {(out or raw)[:600]}")
        # 失败也把「模型实际产出的内容」作为草稿回传：先取抽出的草稿 out，
        # 抽不出（模型只给分析/思考没给 <reply>）则回退原始 raw。
        # 保证单聊「生成过就推微信」——老板要看失败时的内容，哪怕没过质检。
        return ("", (out or raw)) if return_rejected else ""
    return (out, "") if return_rejected else out


def extract_reply(text):
    """从 codebuddy 输出里稳当地取出「要发出的回复正文」。
    deepseek-v4-flash 偶发会进入"助手草稿模式"（给多个选项、反问是否要发），
    用 <reply> 标签 + 兜底规则把真正的回复抽出来，避免把分析文本发到钉钉。"""
    t = (text or "").strip()
    if not t:
        return ""
    # 1) 优先取 <reply>...</reply> 标签内（prompt 已要求模型把正文放这里）
    m = re.search(r"<reply>\s*(.*?)\s*</reply>", t, re.S)
    if m:
        r = m.group(1).strip()
        if r:
            return r
    # 2) 去 markdown 代码围栏
    if t.startswith("```"):
        t = t.strip("`")
        if "\n" in t:
            t = t.split("\n", 1)[1]
    # 3) 取最后一个 > 引用块作兜底（模型常把"建议的草稿"放在 > 后）；
    # 注：此处不与原话比对排除（发件人消息未传入），仅取末个引用块
    quotes = re.findall(r"^\s*>\s?(.*)$", t, re.M)
    if quotes:
        return quotes[-1].strip()
    # 4) 兜底：去掉明显的解释性前缀行，取最后一句像回复的短行
    lines = [l.strip() for l in t.splitlines() if l.strip()]
    meta_prefix = ("我", "按", "建议", "在您", "在你", "是否", "要我", "如果", "注意",
                   "这条", "对方", "示例", "输出：", "现在", "请", "推荐", "综上", "所以")
    cand = [l for l in lines if not l.startswith(meta_prefix) and len(l) <= 80]
    if cand:
        return cand[-1]
    return t.strip()


def _looks_like_reply(text):
    """正向判据：这是一条正常的「老板回复」吗？
    deepseek-v4-flash 偶发会进入"助手草稿/角色扮演"模式输出废话（如 '小张：老板，明天的评审…'、
    '老板，今天下午的会议能改到明天吗'），绝不能发到钉钉；正常反问同事
    （如"要不要我帮你查下?"）现已放行。返回 False 让调用方走安全兜底话术。"""
    if not text:
        return False
    # 放宽长度/换行硬拒：老板回技术问题时本就是多句、较长文本，
    # 旧规则「>150字或含换行即判废」会把正常回复误杀成 skip_reply（见早期 04:13/04:23 案例）。
    # 改为仅对真正失控的超长跑题（>800字，模型明显跑题）判废；
    # 角色扮演/反问/自称老板等精准拦截见下方，不因长度放宽。
    if len(text) > 800:
        return False
    # 最强信号：本人的回复里绝不出现"老板"（人设禁止用"老板"自称/称呼，
    # 真人回同事消息不会写"老板"）。出现即说明模型在角色扮演/助理模式 → 拒。
    if "老板" in text:
        return False
    # 角色扮演/转述标记：开头是「称呼：内容」式（如"小张：""某总："）——模型在转述对方，
    # 不是本人回复。但"注意：""说明：""不过："等是正常口吻，需豁免。
    # 仅匹配 2-4 字纯中文 / 2-6 字英文数字 的称呼+冒号，且不在豁免白名单内。
    _COLON_EXEMPT = {"注意", "说明", "补充", "提醒", "备注", "例如", "比如",
                     "其实", "不过", "另外", "首先", "其次", "最后", "综上", "所以",
                     "如果", "假如", "建议", "正经", "简单", "话说", "对了", "顺便"}
    m = re.match(r"^([一-龥]{2,4}|[A-Za-z0-9]{2,6})[:：]\s*(.*)$", text.strip())
    if m and m.group(1).lower() not in _COLON_EXEMPT:
        return False
    # 澄清/反问类标记：仅拦「模型泄漏系统/转述视角」的明确信号
    # （如把发件人/同事消息当任务复述、暴露"消息：/发件人："等内部字段、或以助理口吻对本人说话）。
    # ⚠️ 2026-07-27 老板再次放宽：此前 07-21 收紧后的"典型助理反问模式"
    # （想让我|要不要我|你希望我|你打算|我建议你|要我帮你|要我帮您…）现已允许代复——
    # 老板回同事本就会反问/主动提出帮忙，这类反问不再拦截，只拦真正的泄漏/角色错乱。
    # ⚠️ 2026-08-03 修复：原正则含「示例」→ agent 查 dws 记录后回复「…示例展示的修改…」
    # （正常业务词，宣传页/文档语境）被误判"示范输出"拦下（实测复现）。「示例」太宽泛，
    # 删掉；模型若真输出"示例回复：…"仍会被 extract_reply 的 meta_prefix / <reply> 标签兜底。
    if re.search(r"(作为您的|我帮您|为您查询|需要我为您|我作为|"
                 r"要我帮你拟|要我(去|帮)拟|消息：|发件人：|同事发|对方：)", text):
        return False
    # ⚠️ 凭印象答事实性问题拦截（2026-07-29 加）：当 agent 跳过 gbrain 直接凭记忆答规格/参数时，
    # 常出现"印象里 / 我记得 / 大概 / 应该是 / 出厂默认 / 印象中"等含糊词。
    # 这种回复违反【严禁编造】+【必须先查 gbrain】，转人工让老板看到草稿更安全。
    # 触发条件：含糊词 + 出现技术参数信号（数字+单位/型号/波特率/电压/电流/频率等）。
    _VAGUE = re.search(r"(印象里|印象中|我记得|大概|大概是|应该是|好像是|似乎|差不多)", text)
    if _VAGUE and re.search(r"(波特率|电压|电流|频率|功率|分辨率|精度|量程|接口|型号|参数|默认值|"
                            r"\d+\s*(bps|kbps|Mbps|V|mV|A|mA|Hz|kHz|MHz|GHz|W|kW|mm|cm|m|°|%|rpm|r/min))",
                            text, re.I):
        return False
    # 反问句：老板回同事时反问/确认细节是正常风格，一律放行
    # （>800字跑题已由上方长度闸拦截，无需在此重复限制）。
    return True


def push_weixin(text, retries=2, retry_wait=3):
    if runtime.DRY_RUN:
        log_debug(f"[DRY_RUN weixin] {text[:120]}")
        return True
    if not SEND_JS or not os.path.exists(SEND_JS) or not NODE or not os.path.exists(NODE):
        # 未安装 weixinclaw-proactive-push skill → 降级为仅日志，不阻断主流程
        log_debug(f"[weixin skipped] {text[:120]}")
        return False
    # ⚠️ 关键：send.js 推送失败时以 exit(1) + 打印 FAILED 退出（非抛异常），
    # 旧版无脑 return True 会静默吞掉失败 → 老板收不到微信却毫无痕迹。
    # 现严格检查返回码 + 输出，失败必留痕（日志 + 审计），便于第一时间定位
    # （典型失败 ret:-2 = 微信 bot 会话失效，需先在微信给 ClawBot 发一条消息激活）。
    # 加固（2026-07-20）：瞬时抖动（网络/限流）自动重试；需人工激活类故障
    # （ret=-2 / prepare failed / 会话已失效）立即失败，不空转重试——
    # 交给上层 GNOTIFY 慢重试或老板激活 ClawBot 后补发。
    NEED_ACTIVATE_HINTS = ("ret=-2", "prepare failed", "会话已失效", "bot id")
    import time as _time
    for attempt in range(retries):
        try:
            r = subprocess.run([NODE, SEND_JS, text], timeout=30,
                               capture_output=True, creationflags=CREATE_NO_WINDOW)
        except Exception as e:
            log_debug(f"weixin push error attempt={attempt+1}/{retries}: {e}")
            if attempt < retries - 1:
                _time.sleep(retry_wait)
                continue
            return False
        out = (r.stdout or b"").decode("utf-8", "replace")
        err = (r.stderr or b"").decode("utf-8", "replace")
        if r.returncode == 0 and "FAILED" not in out and "FAILED" not in err:
            log_debug(f"[weixin ok] {text[:80]}")
            return True
        snippet = (err or out).strip().replace("\n", " ")[:300]
        log_debug(f"[weixin FAIL rc={r.returncode} attempt={attempt+1}/{retries}] {snippet} | text={text[:80]}")
        if any(h in snippet for h in NEED_ACTIVATE_HINTS):
            # 需人工激活，重试无意义，立即失败返回，交由上层处理
            log_audit("weixin_push_fail", text=text[:200], rc=r.returncode, err=snippet[:200])
            return False
        if attempt < retries - 1:
            _time.sleep(retry_wait)
            continue
        log_audit("weixin_push_fail", text=text[:200], rc=r.returncode, err=snippet[:200])
        return False
    return False


def _seed_notify(cid, title, is_single):
    """启动静默 seed 时，对近期(24h)到达的单聊未读发一次被动提醒（不代复）。
    群聊 seed 不提醒（避免开机刷屏；群默认仅 @我才推，downtime 期间非 @我群消息不紧急）。"""
    if not is_single:
        return
    try:
        push_weixin(
            f"📥 启动时发现单聊未读（仅提醒，未代复）：{title}\n"
            f"新消息到达后会正常代复；若老板已读可忽略。"
        )
    except Exception:
        pass
