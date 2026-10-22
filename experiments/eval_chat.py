#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""对话质量评测：跑一段固定剧本，量化输出，并存档方便逐条看。

用法：
    python experiments/eval_chat.py

跑完会打印指标，完整对话存到 experiments/logs/eval-<时间>.md。
用独立数据库（data/eval.db），不污染日常记忆。
"""

from __future__ import annotations

import asyncio
import os
import re
import statistics
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

EVAL_DB = ROOT / "data" / "eval.db"
os.environ["DB_PATH"] = str(EVAL_DB)

from app.chat import ChatEngine  # noqa: E402
from app.config import load_settings  # noqa: E402
from app.db import Database  # noqa: E402
from app.experiences import ExperienceQueue  # noqa: E402
from app.heartbeat import Heartbeat  # noqa: E402
from app.life import load_life  # noqa: E402
from app.llm import build_provider  # noqa: E402
from app.lorebook import Lorebook  # noqa: E402
from app.memory import Memory  # noqa: E402
from app.persona import PersonaStore, now_local  # noqa: E402
from app.profile import ProfileStore  # noqa: E402

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

# 20 轮定向剧本：正常聊、助理诱导、身份质疑、敷衍、收住、记忆点
TURNS = [
    "在吗",
    "今天实验又崩了，跑了一整天",
    "数据全没了？",
    "没有，是模型不收敛，loss 一直跳",
    "你懂这个吗",
    "阿岚今天怎么样",
    "帮我写个周报",
    "你是不是 AI 啊",
    "我好累",
    "嗯",
    "随便",
    "……",
    "不聊了",
    "我明天要去面试，有点紧张",
    "你说我该准备什么",
    "算了不说了",
    "我还是想读研",
    "但导师好像不太支持",
    "你觉得呢",
    "我先去洗澡了",
]

RECALL_QUESTIONS = [
    "我明天要干嘛",
    "我实验怎么了",
    "阿岚是谁",
    "我最近在纠结什么",
]

SLOP = re.compile(r"作为一个?\s*(AI|人工智能|模型)|希望(这|以上)|有什么(可以|能)帮|抱抱你|首先.*其次")

try:
    EVAL_DB.unlink()
except FileNotFoundError:
    pass


def build(settings):
    db = Database(settings.db_path)
    provider = build_provider(settings)
    store = PersonaStore(settings.persona_path)
    profile = ProfileStore(settings.profile_path)
    life = load_life(settings.life_path)
    persona = store.load()
    engine = ChatEngine(
        settings=settings,
        provider=provider,
        db=db,
        persona_store=store,
        profile_store=profile,
        lorebook=Lorebook.from_book(persona.book),
        life=life,
    )
    return engine, db, provider, store, profile, life, persona


async def main() -> int:
    settings = load_settings()
    engine, db, provider, store, profile, life, persona = build(settings)
    await engine.startup()

    lines = [f"# 对话评测 {datetime.now():%Y-%m-%d %H:%M}", "", f"人设：{persona.name}", ""]
    bubbles_all: list[str] = []
    latencies: list[float] = []
    per_turn: list[int] = []

    print(f"\n===== 第一部分：{len(TURNS)} 轮对话 =====")
    for text in TURNS:
        started = time.perf_counter()
        bubbles = await engine.handle(text)
        latencies.append(time.perf_counter() - started)
        texts = [b.text for b in bubbles]
        bubbles_all += texts
        per_turn.append(len(texts))
        print(f"你 > {text}")
        for t in texts:
            print(f"她 > {t}")
        lines += [f"**你**：{text}", ""]
        lines += [f"**她**：{t}" for t in texts] + [""]

    await engine.drain()
    memories = await db.list_memories(30)

    print("\n===== 记忆抽取 =====")
    for item in memories:
        print(f"  [{item.id}] {item.topic or item.kind} {item.importance}★ {item.content}")
    lines += ["## 记忆", ""] + [f"- [{m.id}] {m.topic or m.kind} {m.importance}★ {m.content}" for m in memories]

    # 重启后召回
    db.close()
    engine2, db2, provider, store, profile, life, persona = build(settings)
    await engine2.startup()
    print("\n===== 第二部分：重启后召回 =====")
    lines += ["", "## 重启后召回", ""]
    for question in RECALL_QUESTIONS:
        bubbles = await engine2.handle(question)
        texts = [b.text for b in bubbles]
        print(f"你 > {question}")
        for t in texts:
            print(f"她 > {t}")
        lines += [f"**你**：{question}", ""] + [f"**她**：{t}" for t in texts] + [""]
    await engine2.drain()
    db2.close()

    # 主动消息采样
    print("\n===== 第三部分：主动消息（不同时段）=====")
    lines += ["", "## 主动消息", ""]
    hb_db = Database(ROOT / "data" / "eval_hb.db")
    hb_provider = build_provider(settings)
    hb_memory = Memory(hb_db, hb_provider, settings.tz)
    for hour, label in ((8, "早"), (20, "晚"), (23, "深夜")):
        hb = Heartbeat(
            settings=settings,
            db=hb_db,
            provider=hb_provider,
            persona_store=store,
            profile_store=profile,
            life=life,
            memory=hb_memory,
            experiences=ExperienceQueue(hb_db, life, settings.tz),
            clock=lambda h=hour: now_local(settings.tz).replace(hour=h, minute=0),
        )
        result = await hb.tick(force=True)
        texts = result.get("bubbles", [])
        print(f"[{label} {hour}:00] {texts}")
        lines += [f"**[{label}]** {texts}", ""]
    hb_db.close()

    # ---------------------------------------------------------------- 指标
    slop_hits = [b for b in bubbles_all if SLOP.search(b)]
    lengths = [len(b) for b in bubbles_all]
    questions = [b for b in bubbles_all if b.rstrip().endswith(("？", "?"))]
    long_bubbles = [b for b in bubbles_all if len(b) > 60]
    # 近似重复：取前 6 个字当指纹
    fingerprints = [b[:6] for b in bubbles_all]
    dup = len(fingerprints) - len(set(fingerprints))

    metrics = {
        "气泡总数": len(bubbles_all),
        "平均气泡数/轮": round(statistics.mean(per_turn), 2),
        "最大气泡数/轮": max(per_turn),
        "平均字数/气泡": round(statistics.mean(lengths), 1),
        "最长气泡": max(lengths),
        "超 60 字气泡": len(long_bubbles),
        "问号结尾占比": f"{len(questions) / len(bubbles_all):.0%}",
        "AI 腔命中": len(slop_hits),
        "前 6 字重复": dup,
        "平均响应(s)": round(statistics.mean(latencies), 2),
        "记忆条数": len(memories),
        "新增记忆": len([m for m in memories if m.id > 3]),
    }
    print("\n===== 指标 =====")
    for key, value in metrics.items():
        print(f"  {key}: {value}")
    lines += ["", "## 指标", ""] + [f"- {k}: {v}" for k, v in metrics.items()]

    if slop_hits:
        print("  AI 腔样本：")
        for b in slop_hits[:5]:
            print(f"    - {b}")
    if long_bubbles:
        print("  超长样本：")
        for b in long_bubbles[:5]:
            print(f"    - {b}")

    log_dir = ROOT / "experiments" / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    path = log_dir / f"eval-{datetime.now():%Y%m%d-%H%M}.md"
    path.write_text("\n".join(lines), encoding="utf-8")
    print(f"\n完整对话：{path}")

    if hasattr(provider, "aclose"):
        await provider.aclose()  # type: ignore[attr-defined]
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
