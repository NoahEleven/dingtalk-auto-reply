#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
钉钉自动回复技能 - 自测脚本（集成验证，不真发回复）。

两种模式：
  [默认] 集成验证：以「真实单聊未读」跑通 get_unread → get_latest_msg → gen_reply → reply --dry-run
  [--inject] 注入测试：手动指定一条假消息，验证 gen_reply 生成 + 把回复「发给自己」
            （绝不发给别人）。不依赖真实单聊未读。

【重要】测试消息只发给自己：inject 模式默认把生成结果通过
  dws chat message send --open-dingtalk-id <老板自己的openDingTalkId>
落地到老板自己的会话（文件传输/自己），不会触碰任何其他人的会话。
老板自己的 openDingTalkId 自动从通讯录探测，也可用环境变量 SELF_OPENDINGTALK_ID 指定。

用法：
  # 默认集成验证（DRY_RUN 仅影响 send_reply 是否真发；本脚本默认不真发）
  DRY_RUN=1 python ~/.workbuddy/skills/dingtalk-auto-reply/_validate.py

  # 注入测试（DRY_RUN：只生成+展示"会发给自己"，不真发）
  DRY_RUN=1 python _validate.py --inject --sender "测试同事-张三" --message "在高速，晚点回"

  # 注入测试（真发给自己：去掉 DRY_RUN，回复会真的出现在你自己的钉钉会话里）
  python _validate.py --inject --sender "测试同事-张三" --message "在高速，晚点回"

  # 高级：想测 reply(带引用) 的真实命令形态，指定一个真实会话 id（会发到该会话，慎用于真人）
  python _validate.py --inject --sender 张三 --message 你好 --cid "<openConversationId>"

说明：脚本随技能一起，路径用自身目录解析，可移植。
"""
import sys, os, json, time, argparse

# 用脚本所在目录定位主模块（可移植核心，不硬编码用户名）
HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)
# 测试时开启 dws 实际调用追踪：run_dws 会把真实 argv/路由/返回码记进调试日志
os.environ["DEBUG_DWS_CALL"] = "1"
import dingtalk_unread_monitor as M

# DRY_RUN 来自主模块（环境变量驱动），自测脚本直接复用
DRY_RUN = getattr(M, "DRY_RUN", False)


def print_paths():
    print("=== resolved paths ===")
    print("  DWS_CMD     =", M.DWS_CMD)
    print("  CODEBUDDY_CMD=", M.CODEBUDDY_CMD)
    print("  NODE        =", M.NODE)
    print("  SEND_JS     =", M.SEND_JS)
    print("  PERSONA_FILE=", M.PERSONA_FILE)


def run_dws_check():
    """dws 实际调用比对：把『配置的路径』与『实际 invoked 的命令』对起来，
    并验证 dws 接口是否真能拉到未读。测试时必看。"""
    print("\n=== dws 实际调用比对 (DEBUG_DWS_CALL on) ===")
    e_ok = bool(M.DWS_ENTRY and os.path.exists(M.DWS_ENTRY))
    c_ok = bool(M.DWS_CMD and os.path.exists(M.DWS_CMD))
    n_ok = bool(M.NODE and os.path.exists(M.NODE))
    print(f"  配置: DWS_ENTRY={M.DWS_ENTRY}")
    print(f"        exists   = {e_ok}")
    print(f"        DWS_CMD  = {M.DWS_CMD}")
    print(f"        exists   = {c_ok}")
    print(f"        NODE     = {M.NODE}")
    print(f"        exists   = {n_ok}")
    # 路由优先级（与 M.run_dws 一致）：DIRECT-exe(dws.exe) -> NODE-direct(node+dws.js)
    # -> DWS_CMD-fallback(dws.cmd)。三条路由的输出都统一走文件重定向（dws 对 PIPE
    # 异步 flush 丢失，Python 读 PIPE 恒空；文件重定向是实际主力，与控制台无关）。
    # 这是配置层预判；实际路由以调试日志 [dws-route] 行为准。
    route = ("DIRECT-exe" if (M.DWS_EXE and os.path.exists(M.DWS_EXE))
             else ("NODE-direct" if (e_ok and n_ok)
                   else ("DWS_CMD-fallback" if c_ok else "NONE!! dws 不可用")))
    print(f"  实际路由: {route}")
    print("  实际调用: run_dws(['chat','message','list-unread-conversations','--format','json'])")
    t0 = time.time()
    out = M.run_dws(["chat", "message", "list-unread-conversations", "--format", "json"])
    cost = time.time() - t0
    out_len = len(out or "")
    try:
        data = json.loads(out)
        convs = (data.get("result") or {}).get("conversations") or []
        parsed = len(convs)
    except Exception as e:
        parsed = -1
        print(f"  [warn] 解析失败: {e!r}")
    print(f"  返回: out_len={out_len} 解析会话数={parsed} 耗时={cost:.2f}s")
    print(f"  比对: 配置exists(ENTRY={e_ok},CMD={c_ok},NODE={n_ok}) <-> 实际路由={route} <-> 接口返回len={out_len}")
    verdict = "PASS" if (route != "NONE!! dws 不可用" and out_len > 0) else ("WARN(空返回)" if route != "NONE!! dws 不可用" else "FAIL(dws 不可用)")
    print(f"  结论: {verdict}")
    print("  （详细 argv/rc 见调试日志 ~/.workbuddy/dingtalk_auto_debug.log 的 [dws-call]/[dws-route] 行；")
    print("   实际数据抓取走文件重定向，日志表现为 [dws-file] OK out_len=N）")


def run_integration():
    """默认：以真实单聊未读跑通整条链路。"""
    print("\n=== get_unread (dws list-unread-conversations) ===")
    convs = M.get_unread()
    for c in convs:
        print("  -", c.get("title"), "single=", c.get("singleChat"), "unread=", c.get("unreadPoint"))

    target = next((c for c in convs if c.get("singleChat")), None)
    if not target:
        print("\n[skip] 当前无单聊未读，跳过集成验证（群聊不代发，符合预期）")
        print("        想完整验证生成链路可用 --inject 模式：")
        print("        python _validate.py --inject --sender 张三 --message 你好")
        return

    cid = target["openConversationId"]
    print("\n=== target single chat:", target.get("title"), "cid=", cid)
    msg = M.get_latest_msg(cid)
    print("  msg_id   =", M.msg_field(msg, "openMessageId", "messageId", "msgId"))
    print("  sender   =", M.msg_field(msg, "sender", "senderName", "senderNick"))
    print("  senderOpenDingTalkId =", M.msg_field(msg, "senderOpenDingTalkId", "openDingTalkId", "fromOpenDingTalkId"))
    content = M._as_text(M.msg_field(msg, "content", "text", "body", default=""))
    print("  content  =", repr(content[:80]))

    print("\n=== gen_reply (CodeBuddy SDK, 真实生成一次, 不发送) ===")
    reply = M.gen_reply(
        M.msg_field(msg, "sender", "senderName"),
        content if content.strip() else "(图片/媒体消息)",
    )
    print("  REPLY >>>", reply)

    print("\n=== dws reply --dry-run (校验命令形态, 不真发) ===")
    args = ["chat", "message", "reply",
            "--conversation-id", cid,
            "--ref-msg-id", M.msg_field(msg, "openMessageId", "messageId", "msgId"),
            "--ref-sender", M.msg_field(msg, "senderOpenDingTalkId", "openDingTalkId", "fromOpenDingTalkId"),
            "--text", reply or M.FALLBACK_REPLY, "--yes", "--dry-run"]
    out = M.run_dws(args)
    print(out[:600])
    print("\n=== validate done ===")


def run_inject(sender, message, cid):
    """注入测试：手动假消息跑 gen_reply，再把回复「发给自己」（绝不发给别人）。"""
    print(f"\n=== INJECT MODE（测试消息只发给自己）===")
    print(f"  sender  = {sender!r}")
    print(f"  message = {message!r}")

    print("\n=== gen_reply (CodeBuddy SDK, 真实生成, 不发送) ===")
    t0 = time.time()
    reply = M.gen_reply(sender, message)
    cost = time.time() - t0
    print(f"  returned in {cost:.1f}s")
    print("  REPLY >>>", reply)

    self_id = M.get_self_openid()
    print("\n=== 目标：老板自己 (SELF) ===")
    print("  self openDingTalkId =", self_id or "(未检测到，可用 SELF_OPENDINGTALK_ID 指定)")

    if cid:
        # 高级路径：用户显式指定真实会话，测 reply(带引用) 命令形态（会发到该会话，慎用于真人）
        print(f"\n[advanced] --cid 指定了真实会话 {cid}，做 reply --dry-run（不真发）")
        msg = M.get_latest_msg(cid)
        ref_id = M.msg_field(msg, "openMessageId", "messageId", "msgId")
        ref_sender = M.msg_field(msg, "senderOpenDingTalkId", "openDingTalkId", "fromOpenDingTalkId")
        if not ref_id:
            print("  [warn] 该会话取不到最新消息 ref，跳过 reply --dry-run。")
        else:
            out = M.run_dws(["chat", "message", "reply", "--conversation-id", cid,
                             "--ref-msg-id", ref_id, "--ref-sender", ref_sender,
                             "--text", reply or M.FALLBACK_REPLY, "--yes", "--dry-run"])
            print(out[:600])
        print("\n=== inject done ===")
        return

    # 默认路径：把回复发给自己（安全，绝不会打扰别人）
    if DRY_RUN:
        print("\n[DRY_RUN] 不会真发。去掉 DRY_RUN=1 后，上面的回复会直接出现在你自己的钉钉会话里。")
    else:
        if not self_id:
            print("\n[abort] 未检测到自己的 openDingTalkId，无法发给自己。")
            print("         设置环境变量 SELF_OPENDINGTALK_ID=你的openDingTalkId 后重试。")
        else:
            ok = M.send_reply_self(reply or M.FALLBACK_REPLY)
            print(f"\n[send to self] ok={ok}  -> 已把测试回复发到你自己的钉钉会话")
    print("\n=== inject done ===")


def run_guard_test(cid):
    """验证抢答防护逻辑（活跃检测 + 延迟窗口），不真发任何消息、不触碰任何人会话。"""
    print("\n=== GUARD TEST（抢答防护逻辑验证，不真发）===")
    if not cid:
        cid = M.find_single_conversation(7)
        print("  auto-detected single-chat cid =", cid or "(无)")
    if not cid:
        print("  [skip] 没有可用的单聊会话用于测试，可用 --cid 指定")
        return
    # 1) 活跃检测：老板最近是否在该会话发过消息
    print("\n[1] owner_recently_active(300s) =", M.owner_recently_active(cid, 300))
    print("    True  = 老板最近5分钟在该会话发过消息 → 会跳过代发（他在跟，会自己回）")
    print("    False = 老板不活跃 → 会进延迟窗口，等窗口结束才代发")
    # 2) 延迟窗口演示：只生成回复不真发
    print(f"\n[2] 模拟「发现未读 → 进 REPLY_DELAY_SEC 延迟窗口 → 到期代发」")
    print(f"    REPLY_DELAY_SEC = {M.REPLY_DELAY_SEC}s, ACTIVE_WINDOW_SEC = {M.ACTIVE_WINDOW_SEC}s")
    print("    DRY_RUN 下只生成回复不真发；真实运行会等延迟窗口结束才发（期间老板回了就取消）")
    reply = M.gen_reply("测试同事", "在吗？方案发你邮箱了")
    print("    若老板在窗口内未回复，将代发：", reply or "(生成失败 → 转人工通知)")
    print("\n=== guard test done ===")


def _warn_sdk_missing():
    print("\n[!!] CodeBuddy Agent SDK 未安装或无法导入（_SDK_AVAILABLE=False）。")
    print("    gen_reply 将静默返回空 -> 不会代发。请先安装到运行本脚本的 python：")
    print("      Windows: %USERPROFILE%\\.workbuddy\\binaries\\python\\envs\\default\\Scripts\\python.exe -m pip install codebuddy-agent-sdk")
    print("      POSIX  : $HOME/.workbuddy/binaries/python/envs/default/bin/python3 -m pip install codebuddy-agent-sdk")
    print("    注意：必须用『装了 SDK 的 default venv』跑本脚本，裸 managed python 不含 SDK。\n")


def main():
    if not getattr(M, "_SDK_AVAILABLE", False):
        _warn_sdk_missing()
    parser = argparse.ArgumentParser(description="钉钉自动回复自测（集成 / 注入）")
    parser.add_argument("--inject", action="store_true", help="注入测试模式：手动假消息跑 gen_reply + reply --dry-run")
    parser.add_argument("--sender", default="测试同事", help="注入消息的发件人（仅用于生成上下文，不真发给此人）")
    parser.add_argument("--message", default="在吗？方案发你邮箱了", help="注入消息的内容")
    parser.add_argument("--cid", default=None, help="真实单聊会话ID，用于 reply --dry-run / --test-guard；缺省自动探测")
    parser.add_argument("--test-guard", action="store_true", help="验证抢答防护逻辑(活跃检测+延迟窗口)，不真发")
    args = parser.parse_args()

    print_paths()
    run_dws_check()
    if args.test_guard:
        run_guard_test(args.cid)
    elif args.inject:
        run_inject(args.sender, args.message, args.cid)
    else:
        run_integration()


if __name__ == "__main__":
    main()
