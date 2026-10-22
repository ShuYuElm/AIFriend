#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""prompt_lab.py — 人设实验台（零依赖，仅标准库）

在写正式代码之前，用这一个文件回答一个问题：她像不像一个人？

用法（PowerShell）:
    $env:DEEPSEEK_API_KEY = "sk-xxxxxxxx"
    python experiments/prompt_lab.py

或者把 key 写进项目根目录的 .env：
    DEEPSEEK_API_KEY=sk-xxxxxxxx

命令:
    /reload   重新读取 prompt.md（默认会自动检测改动并重载）
    /clear    清空上下文，重新开始
    /stat     看上下文规模与当前参数
    /temp 0.9 调温度（0.5 稳 / 0.9 自然 / 1.2 飘）
    /dry      打印最终 system prompt（不请求模型）
    /save     手动存档
    /help     看命令
    /quit     退出（自动存档）
"""

import json
import os
import sys
from datetime import datetime, timedelta, timezone
from http.client import HTTPSConnection
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PROMPT_FILE = ROOT / "prompt.md"
LOG_DIR = ROOT / "logs"

HOST = "api.deepseek.com"
MODEL = os.environ.get("LAB_MODEL", "deepseek-chat")
MAX_HISTORY = 24          # 只保留最近 N 条，模拟正式版的 L3 窗口
MAX_TOKENS = 300          # 强制她别写长文
DEFAULT_TEMPERATURE = 0.9
WEEKDAYS = "一二三四五六日"

try:  # Windows 控制台的编码兜底
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stdin.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass


# ---------------------------------------------------------------- 基础设施

def local_now() -> datetime:
    """优先用 Asia/Shanghai；Windows 没装 tzdata 时退回固定 +8。"""
    try:
        from zoneinfo import ZoneInfo

        return datetime.now(ZoneInfo("Asia/Shanghai"))
    except Exception:
        return datetime.now(timezone(timedelta(hours=8)))


def load_dotenv() -> None:
    """极简 .env 读取：只看 experiments/.env 和项目根 .env。"""
    for path in (ROOT / ".env", ROOT.parent / ".env"):
        if not path.exists():
            continue
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


_prompt_mtime = -1.0
PROMPT_TEXT = ""


def load_prompt(force: bool = False) -> bool:
    """按 mtime 缓存人设；返回是否发生了重载。"""
    global PROMPT_TEXT, _prompt_mtime
    if not PROMPT_FILE.exists():
        print(f"还没有人设文件：{PROMPT_FILE}")
        print("  她是谁只能你来定。")
        print("  · 先填 experiments/persona-canvas.md，然后让我生成 prompt.md")
        print("  · 或者直接口述，我来整理成卡给你确认")
        print("  · 想先试试手感，可以把 prompt_example.md 复制成 prompt.md（但那是别人的她）")
        sys.exit(2)
    mtime = PROMPT_FILE.stat().st_mtime
    if force or mtime != _prompt_mtime:
        PROMPT_TEXT = PROMPT_FILE.read_text(encoding="utf-8").strip()
        _prompt_mtime = mtime
        return True
    return False


def display_name() -> str:
    """从人设文件的一级标题取名字，没有就退回"她"。"""
    for line in PROMPT_TEXT.splitlines():
        if line.startswith("# "):
            return line[2:].strip()
    return "她"


def runtime_context(gap_seconds):
    """注入"现在几点、隔了多久"，这是她显得有现实感的关键。"""
    now = local_now()
    lines = [f"现在是 {now:%Y-%m-%d} 周{WEEKDAYS[now.weekday()]} {now:%H:%M}。"]
    if gap_seconds is None:
        lines.append("这是你们今天第一次说话。")
    else:
        minutes = int(gap_seconds // 60)
        if minutes < 1:
            lines.append("他刚发来消息。")
        elif minutes < 60:
            lines.append(f"上一句是他 {minutes} 分钟前说的。")
        else:
            lines.append(f"上一句是他 {minutes // 60} 小时 {minutes % 60} 分钟前说的。")
    return "\n".join(lines)


def build_system(gap_seconds):
    return (
        PROMPT_TEXT
        + "\n\n---\n\n## 现在的情况\n\n"
        + runtime_context(gap_seconds)
        + "\n\n（这些只是背景，不要在回复里复述时间。）"
    )


class ApiError(RuntimeError):
    pass


def stream_chat(messages, temperature, on_delta=None):
    """流式请求，边收边打印；返回完整文本。"""
    api_key = os.environ.get("DEEPSEEK_API_KEY", "").strip()
    if not api_key:
        raise ApiError("缺少 DEEPSEEK_API_KEY")

    payload = json.dumps(
        {
            "model": MODEL,
            "messages": messages,
            "stream": True,
            "temperature": temperature,
            "max_tokens": MAX_TOKENS,
        },
        ensure_ascii=False,
    ).encode("utf-8")

    conn = HTTPSConnection(HOST, timeout=90)
    try:
        conn.request(
            "POST",
            "/chat/completions",
            body=payload,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {api_key}",
                "Accept": "text/event-stream",
            },
        )
        resp = conn.getresponse()
        if resp.status != 200:
            detail = resp.read().decode("utf-8", "replace")[:600]
            raise ApiError(f"HTTP {resp.status}：{detail}")

        pieces = []
        for raw in resp:
            line = raw.decode("utf-8", "replace").strip()
            if not line or not line.startswith("data:"):
                continue
            data = line[5:].strip()
            if data == "[DONE]":
                break
            try:
                obj = json.loads(data)
            except json.JSONDecodeError:
                continue
            for choice in obj.get("choices") or []:
                piece = (choice.get("delta") or {}).get("content") or ""
                if not piece:
                    continue
                pieces.append(piece)
                if on_delta:
                    on_delta(piece)
        return "".join(pieces)
    finally:
        conn.close()


# ---------------------------------------------------------------- 交互

def estimate_tokens(text: str) -> int:
    """粗估：中文约 0.7 token/字，英文约 0.3。"""
    chinese = sum(1 for ch in text if "\u4e00" <= ch <= "\u9fff")
    other = len(text) - chinese
    return int(chinese * 0.7 + other * 0.3)


def main() -> int:
    load_dotenv()
    load_prompt(force=True)

    if "--dry" in sys.argv:
        print(build_system(None))
        print("\n--- 以上是最终 system prompt ---")
        return 0

    has_key = bool(os.environ.get("DEEPSEEK_API_KEY", "").strip())
    started = local_now()
    log_file = LOG_DIR / f"{started:%Y%m%d-%H%M}.md"

    print()
    print(f"  {display_name()} · 人设实验台")
    print("  人设   experiments/prompt.md  （改完自动重载）")
    print(f"  模型   {MODEL}   温度 {DEFAULT_TEMPERATURE}")
    print("  输入 /help 看命令 · Ctrl+C 退出并自动存档")
    print()

    if not has_key:
        print("  ! 没有找到 DEEPSEEK_API_KEY")
        print("    PowerShell：$env:DEEPSEEK_API_KEY = \"sk-xxxxxxxx\"")
        print("    或写进项目根目录 .env：DEEPSEEK_API_KEY=sk-xxxxxxxx")
        print()
        return 2

    history: list[dict] = []
    last_user_at = None
    temperature = DEFAULT_TEMPERATURE

    def archive() -> None:
        lines = [
            f"# {display_name()} · 实验记录",
            "",
            f"- 时间：{started:%Y-%m-%d %H:%M}",
            f"- 模型：{MODEL}　温度：{temperature}",
            "",
            "---",
            "",
        ]
        for msg in history:
            who = "你" if msg["role"] == "user" else display_name()
            lines.append(f"**{who}**：{msg['content']}")
            lines.append("")
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        log_file.write_text("\n".join(lines), encoding="utf-8")

    def handle_command(cmd: str):
        nonlocal last_user_at, temperature, history
        name, _, arg = cmd.partition(" ")
        name = name.lower()

        if name in ("/quit", "/exit", "/q"):
            return "quit"
        if name == "/help":
            print("  /reload  /clear  /stat  /temp 0.9  /dry  /save  /quit")
        elif name == "/reload":
            load_prompt(force=True)
            print("  （人设已重新载入）")
        elif name == "/clear":
            history = []
            last_user_at = None
            print("  （上下文已清空）")
        elif name == "/stat":
            text = "\n".join(m["content"] for m in history)
            print(f"  上下文 {len(history)} 条 · 约 {len(text)} 字 · 约 {estimate_tokens(text)} tokens")
            print(f"  窗口上限 {MAX_HISTORY} 条 · 温度 {temperature}")
        elif name == "/temp":
            try:
                value = float(arg)
            except ValueError:
                print("  用法：/temp 0.9")
                return None
            temperature = max(0.0, min(1.5, value))
            print(f"  （温度 → {temperature}）")
        elif name == "/dry":
            print()
            print(build_system(None))
            print()
        elif name == "/save":
            archive()
            print(f"  （已存档 {log_file}）")
        else:
            print(f"  未知命令 {name}，输入 /help")
        return None

    while True:
        try:
            text = input("\n你 > ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break

        if not text:
            continue
        if text.startswith("/"):
            if handle_command(text) == "quit":
                break
            continue

        if load_prompt():
            print("  （检测到人设改动，已重新载入）")

        gap = None
        if last_user_at is not None:
            gap = (local_now() - last_user_at).total_seconds()

        history.append({"role": "user", "content": text})
        messages = [{"role": "system", "content": build_system(gap)}] + history[-MAX_HISTORY:]

        sys.stdout.write("\n她 > ")
        sys.stdout.flush()
        try:
            reply = stream_chat(messages, temperature, on_delta=lambda p: (sys.stdout.write(p), sys.stdout.flush()))
        except ApiError as exc:
            print(f"\n  [请求失败] {exc}")
            history.pop()
            continue
        except Exception as exc:  # 断网、超时之类
            print(f"\n  [异常] {type(exc).__name__}: {exc}")
            history.pop()
            continue

        print()
        if not reply.strip():
            print("  [空回复]")
            history.pop()
            continue

        history.append({"role": "assistant", "content": reply})
        last_user_at = local_now()
        archive()

    archive()
    print(f"\n已存档：{log_file}\n")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(0)
