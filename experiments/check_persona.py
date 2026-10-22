#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""check_persona.py — 人设卡体检

校验 config/persona.yaml 与 config/life.yaml：
  结构是否符合人设卡与日程约定
  内容是否踩了常见的"AI 味"和"设定矛盾"坑

用法:
    python experiments/check_persona.py
"""

import re
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.config import config_file  # noqa: E402
from app.lorebook import estimate_tokens  # noqa: E402

# 和程序用同一套解析：config/*.local.yaml 存在就查它，否则查示例
PERSONA = config_file("persona")
LIFE = config_file("life")
PROFILE = config_file("user_profile")

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

errors: list[str] = []
warns: list[str] = []
oks: list[str] = []


def ok(msg: str) -> None:
    oks.append(msg)


def warn(msg: str) -> None:
    warns.append(msg)


def err(msg: str) -> None:
    errors.append(msg)


# ------------------------------------------------------------------ 工具

AI_SLOP = [
    (r"作为一个?(大)?(语言)?模型", "自称 AI"),
    (r"作为一个?\s*(AI|人工智能|助手|机器人|聊天机器人)", "自称 AI"),
    (r"作为\s*(AI|人工智能)", "自称 AI"),
    (r"我理解你的(感受|心情)", "心理客服腔"),
    (r"希望(这|以上|这些)?(对|能帮)", "助手结语"),
    (r"有什么(可以|能)帮(你|到)", "客服开场"),
    (r"我(会)?(一直|随时)(在|陪)着你", "陪伴承诺腔"),
    (r"抱抱你", "泛化安慰"),
    (r"首先[，,].*其次", "列点式说理"),
    (r"总的来说|总而言之", "总结腔"),
]

SELF_INTRO = re.compile(r"你好|我是|很高兴认识|嗨[，,]|在吗")


def to_min(hhmm: str) -> int:
    h, m = hhmm.split(":")
    return int(h) * 60 + int(m)


NEGATION = re.compile(r"(绝)?(不|没|别)(会)?(说|讲|用|提|要)|禁止|避免|从不")


def scan_slop(text: str, where: str) -> None:
    for pattern, label in AI_SLOP:
        for m in re.finditer(pattern, text):
            head = text[max(0, m.start() - 20):m.start()]
            if NEGATION.search(head):
                continue  # "她绝不说作为 AI" 是禁令，不是使用
            err(f"{where} 命中「{label}」：{m.group(0)!r} ← …{text[max(0, m.start() - 20):m.end() + 4]}…")
            break


# ------------------------------------------------------------------ persona

def check_persona() -> dict:
    data = yaml.safe_load(PERSONA.read_text(encoding="utf-8"))

    if data.get("spec") != "aigirl_card_v1":
        err(f"persona.spec 应为 aigirl_card_v1，实际 {data.get('spec')!r}")

    required = [
        "name", "description", "personality", "scenario",
        "system_prompt", "post_history_instructions",
        "first_mes", "alternate_greetings", "mes_example", "character_book",
    ]
    missing = [k for k in required if not data.get(k)]
    if missing:
        err(f"persona 缺字段：{', '.join(missing)}")
    else:
        ok(f"persona 字段齐全（{len(required)} 项）")

    # first_mes
    fm = str(data.get("first_mes", "")).strip()
    if len(fm) > 20:
        warn(f"first_mes 偏长（{len(fm)} 字），建议 ≤15 字")
    if SELF_INTRO.search(fm):
        err(f"first_mes 像自我介绍：{fm!r}")
    else:
        ok(f"first_mes 不像自我介绍：{fm!r}")

    # mes_example
    mes = str(data.get("mes_example", ""))
    blocks = [b for b in mes.split("\n\n") if b.strip()]
    char_lines = [ln for ln in mes.splitlines() if ln.startswith("<char>")]
    multi = any(
        len([ln for ln in b.splitlines() if ln.startswith("<char>")]) >= 2
        for b in blocks
    )
    if not multi:
        err("mes_example 没有任何一组连发多条 <char>，模型学不到多气泡节奏")
    else:
        ok(f"mes_example 有 {len(blocks)} 组、{len(char_lines)} 条 <char>，含连发")

    # character_book
    book = data.get("character_book") or {}
    entries = book.get("entries") or []
    if not (3 <= len(entries) <= 8):
        warn(f"lorebook 条目 {len(entries)} 条，建议 3–8 条")
    constant = [e for e in entries if e.get("constant")]
    if len(constant) > 2:
        err(f"lorebook 常驻条目 {len(constant)} 条，建议 ≤2（常驻会吃满 token 预算）")

    for e in entries:
        content = str(e.get("content", ""))
        if len(content) > 40:
            warn(f"lorebook 条目偏长（{len(content)} 字）：{content[:20]}…")
        if re.search(r"曾|那次|之前我们|我们一起|见过", content):
            warn(f"lorebook 可能写了『关系事件』（应进动态记忆）：{content[:24]}")

    # 语气字段
    for field in ("personality", "system_prompt", "mes_example"):
        scan_slop(str(data.get(field, "")), f"persona.{field}")

    # 表情包
    if "表情包" in str(data.get("personality", "")):
        warn("personality 提到『表情包』，但第一版只发文字；建议改成『偶尔发 [狗头] 这类文字表情』")

    return data


# ------------------------------------------------------------------ life

def check_life() -> dict:
    data = yaml.safe_load(LIFE.read_text(encoding="utf-8"))
    schedule = data.get("schedule") or {}

    for group, segs in schedule.items():
        if not segs:
            err(f"life.schedule.{group} 为空")
            continue
        parsed_raw = sorted((to_min(s["from"]), to_min(s["to"]), s) for s in segs)
        # 拆开跨零点的段，例如 22:30–00:55 → [22:30,24:00) + [00:00,00:55)
        parsed = []
        for start, end, seg in parsed_raw:
            if end <= start:
                parsed.append((start, 24 * 60, seg))
                parsed.append((0, end, seg))
            else:
                parsed.append((start, end, seg))
        parsed.sort(key=lambda x: x[0])

        cursor = 0
        seamless = True
        for start, end, seg in parsed:
            if start != cursor:
                gap = start - cursor
                kind = "空隙" if gap > 0 else "重叠"
                err(f"life.schedule.{group} 在 {cursor // 60:02d}:{cursor % 60:02d} 处有{kind}（{abs(gap)} 分钟）")
                seamless = False
            cursor = max(cursor, end)
        if cursor != 24 * 60:
            err(f"life.schedule.{group} 未覆盖到 24:00（停在 {cursor // 60:02d}:{cursor % 60:02d}）")
            seamless = False
        if seamless:
            ok(f"life.schedule.{group} 覆盖满 24 小时且无缝（{len(segs)} 段）")

        for seg in segs:
            if not str(seg.get("detail", "")).strip() and "睡" not in str(seg.get("state", "")):
                warn(f"{group} 的 {seg['from']}-{seg['to']} 段没有 detail，主动消息会没细节可说")

    # 作息 → 建议 quiet_hours
    bedtime, wake = None, None
    for seg in schedule.get("mon-fri", []):
        if "睡" in str(seg.get("state", "")):
            bedtime, wake = seg["from"], seg["to"]
    if bedtime and wake:
        ok(f"她通常 {bedtime} 睡、{wake} 起 → 建议 quiet_hours = ({bedtime}, {wake})")

    events = data.get("events") or []
    if not (5 <= len(events) <= 10):
        warn(f"events {len(events)} 条，建议 5–10 条")
    for ev in events:
        text = str(ev)
        if len(text) > 50:
            warn(f"event 偏长（{len(text)} 字）：{text[:20]}…")
        if re.search(r"我刚|刚刚|刚才|今天", text):
            warn(f"event 用了即时时间词『我刚/今天』，延迟分享时会产生矛盾：{text[:20]}…")
    return data


def check_profile() -> None:
    if not PROFILE.exists():
        warn("没有 user_profile.yaml，她将不知道任何关于他的事")
        return

    raw = yaml.safe_load(PROFILE.read_text(encoding="utf-8")) or {}
    facts: list[str] = []
    for section in raw.get("sections") or []:
        facts += [str(f).strip() for f in (section.get("facts") or [])]
    facts += [str(f).strip() for f in (raw.get("facts") or [])]

    usable = [f for f in facts if f and not any(m in f for m in ("___", "TODO", "待填"))]
    blanks = len(facts) - len(usable)
    if blanks:
        warn(f"user_profile 还有 {blanks} 条占位未填")

    if not usable:
        warn("user_profile 没有可注入的事实")
        return

    if len(usable) > 40:
        err(f"user_profile {len(usable)} 条，超过 40 条上限，多余的不注入")

    dup = {f for f in usable if usable.count(f) > 1}
    for fact in dup:
        warn(f"user_profile 重复条目：{fact[:20]}")

    if not any("生日" in f for f in usable):
        warn("user_profile 没有生日，M3 的生日主动消息用不了")

    tokens = estimate_tokens("\n".join(usable))
    if tokens > 600:
        warn(f"user_profile 估算 {tokens} token，建议压缩到 600 以内")
    else:
        ok(f"user_profile {len(usable)} 条，约 {tokens} token")


# ------------------------------------------------------------------ 交叉

def check_cross(persona: dict, life: dict) -> None:
    book = (persona.get("character_book") or {}).get("entries") or []
    life_text = LIFE.read_text(encoding="utf-8")
    for e in book:
        m = re.search(r"(\d{1,2}):(\d{2})\s*[到至—\-–~]\s*(\d{1,2}):(\d{2})", str(e.get("content", "")))
        if m:
            start, end = m.group(1) + ":" + m.group(2), m.group(3) + ":" + m.group(4)
            if start not in life_text or end not in life_text:
                warn(f"lorebook 写了作息 {start}-{end}，但 life.yaml 里找不到对应时间")

    # 用户称呼
    scenario = str(persona.get("scenario", ""))
    names = re.findall(r"与([\u4e00-\u9fa5]{1,3})在", scenario)
    if names:
        ok(f"scenario 里用户被称作「{names[0]}」——确认这是你要的称呼")


# ------------------------------------------------------------------ main

def main() -> int:
    if not PERSONA.exists() or not LIFE.exists():
        print(f"找不到配置文件：{PERSONA} / {LIFE}")
        return 2

    persona = check_persona()
    life = check_life()
    check_profile()
    check_cross(persona, life)

    print()
    for line in oks:
        print(f"  [OK]   {line}")
    for line in warns:
        print(f"  [WARN] {line}")
    for line in errors:
        print(f"  [FAIL] {line}")
    print()
    print(f"  合计：{len(oks)} 通过 / {len(warns)} 警告 / {len(errors)} 错误")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
