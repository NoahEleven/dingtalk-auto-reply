#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""_setup_env.py —— 安装时探测并写入 SDK 运行环境到 .env。

目的：让任何 agent（或人）在装好本 skill 后跑一次本脚本，即把
「装了 codebuddy-agent-sdk 的 python 解释器路径 + 主模型 + 网络环境 + CLI 路径」
写入技能目录下的 .env。这样：
  · skill 运行时能按 CODEBUDDY_SDK_PYTHON 自拉起到正确的 python（见 runtime.sdk_reexec_target）；
  · 后续 agent 读 .env 即可「识别」SDK 环境，不必重新探测。

用法：
  python _setup_env.py            # 探测并写入（不覆盖已手填的值；仅填充空项/新增）
  python _setup_env.py --force    # 强制覆盖 SDK 相关项（CODEBUDDY_SDK_PYTHON/MODEL/INTERNET_ENVIRONMENT/CMD）
  python _setup_env.py --check    # 只打印探测结果，不写文件
"""
import os
import sys
import glob
import shutil
import subprocess
import argparse

_SKILL_DIR = os.path.dirname(os.path.abspath(__file__))
_ENV_PATH = os.path.join(_SKILL_DIR, ".env")
_ENV_EXAMPLE = os.path.join(_SKILL_DIR, ".env.example")


def _candidate_pythons():
    """候选 python：受管 venv / versions 目录 + 当前解释器，去重保序。"""
    home = os.path.expanduser("~")
    pats = [
        os.path.join(home, ".workbuddy", "binaries", "python", "envs", "*",
                     "Scripts" if sys.platform.startswith("win") else "bin",
                     "python.exe" if sys.platform.startswith("win") else "python3"),
        os.path.join(home, ".workbuddy", "binaries", "python", "versions", "*",
                     "python.exe" if sys.platform.startswith("win") else "python"),
    ]
    cands = []
    for p in pats:
        cands.extend(sorted(glob.glob(p), reverse=True))
    cands.append(sys.executable)
    seen, out = set(), []
    for c in cands:
        c = os.path.abspath(c)
        if c not in seen:
            seen.add(c)
            out.append(c)
    return out


def _python_has_sdk(py):
    try:
        r = subprocess.run([py, "-c", "import codebuddy_agent_sdk; print('OK')"],
                           capture_output=True, text=True, timeout=30)
        return r.returncode == 0 and "OK" in r.stdout
    except Exception:
        return False


def _detect_sdk_python():
    for py in _candidate_pythons():
        if _python_has_sdk(py):
            return py
    return ""


def _detect_via_runtime(fn, default=""):
    """安全地复用 runtime 的探测函数（避免重复实现）。"""
    try:
        if _SKILL_DIR not in sys.path:
            sys.path.insert(0, _SKILL_DIR)
        import runtime as _rt
        return fn(_rt)
    except Exception:
        return default


def _read_existing_env(path):
    vals = {}
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            for line in f:
                s = line.strip()
                if not s or s.startswith("#") or "=" not in s:
                    continue
                k, v = s.split("=", 1)
                k, v = k.strip(), v.strip()
                if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
                    v = v[1:-1]
                vals[k] = v
    return vals


def _write_env(entries, force=False):
    """把 entries 写入 .env：
    · 已存在且值为空（KEY= 或注释占位）的项 → 填充；
    · 已存在且用户填了值 → 仅 --force 覆盖；
    · 不存在的项 → 追加到末尾（带注释）。
    其余行原样保留。"""
    if not os.path.exists(_ENV_PATH):
        if os.path.exists(_ENV_EXAMPLE):
            shutil.copy(_ENV_EXAMPLE, _ENV_PATH)
            print("[setup] 已从 .env.example 创建 .env")
        else:
            open(_ENV_PATH, "w", encoding="utf-8").close()
    with open(_ENV_PATH, encoding="utf-8") as f:
        lines = f.readlines()
    existing = _read_existing_env(_ENV_PATH)
    out, written = [], set()
    for ln in lines:
        s = ln.strip()
        key = s.split("=", 1)[0].strip() if "=" in s else ""
        if key in entries:
            cur = existing.get(key, "")
            if force or cur == "":
                out.append(f"{key}={entries[key]}\n")
                written.add(key)
                continue
        out.append(ln if ln.endswith("\n") else ln + "\n")
    # 追加文件中尚不存在的键
    for k, v in entries.items():
        if k not in written:
            out.append(f"\n# --- 安装时自动探测写入（_setup_env.py）---\n{k}={v}\n")
            written.add(k)
    with open(_ENV_PATH, "w", encoding="utf-8") as f:
        f.writelines(out)


def main():
    ap = argparse.ArgumentParser(description="安装时探测并写入 SDK 运行环境到 .env")
    ap.add_argument("--force", action="store_true", help="强制覆盖 SDK 相关项")
    ap.add_argument("--check", action="store_true", help="只打印探测结果，不写文件")
    args = ap.parse_args()

    sdk_py = _detect_sdk_python()
    china = _detect_via_runtime(lambda rt: rt._detect_china_edition(), False)
    cmd = _detect_via_runtime(lambda rt: rt.CODEBUDDY_CMD, "")
    model = os.environ.get("CODEBUDDY_MODEL", "hy3")
    print(f"[detect] SDK python            = {sdk_py or '(未找到)'}")
    print(f"[detect] 中国版(internal)      = {china}")
    print(f"[detect] codebuddy CLI          = {cmd or '(自动探测)'}")
    print(f"[detect] 主模型(文本+视觉)     = {model}")

    if args.check:
        return 0
    if not sdk_py:
        print("\n[!!] 未找到装了 codebuddy-agent-sdk 的 python。")
        print("    请先安装：<目标venv的python> -m pip install codebuddy-agent-sdk")
        print("    再重跑本脚本。")
        return 2

    entries = {
        "CODEBUDDY_SDK_PYTHON": sdk_py,
        "CODEBUDDY_MODEL": model,
        "CODEBUDDY_INTERNET_ENVIRONMENT": "internal" if china else "public",
    }
    if cmd:
        entries["CODEBUDDY_CMD"] = cmd

    _write_env(entries, force=args.force)
    keys = " / ".join(entries.keys())
    print(f"\n[done] 已将 SDK 运行环境写入 .env：\n       {keys}")
    print("       monitor 启动时会按 CODEBUDDY_SDK_PYTHON 自拉起到正确 python。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
