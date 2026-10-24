"""风格包对照实验：同一段对话，standard / human 各跑一遍。

用真实模型、临时数据库（不碰 data/aigirl.db），同一组用户台词、同一时钟。
跑完把并排结果写成 markdown，放在 experiments/logs/ 下（已 gitignore）。

用法：
    .\\.venv\\Scripts\\python.exe experiments/ab_packs.py
    .\\.venv\\Scripts\\python.exe experiments/ab_packs.py --turns 6
"""

from __future__ import annotations

import argparse
import asyncio
import dataclasses
import re
import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.chat import ChatEngine
from app.config import ROOT, load_settings
from app.db import Database
from app.life import load_life
from app.llm import build_provider
from app.lorebook import Lorebook
from app.persona import PersonaStore, now_local
from app.profile import ProfileStore
from app.prompts import STATE_KEY, PromptPacks
from app.search import SearchClient

TURNS = [
    "今天好累，刚下班",
    "地铁上看到一个人穿的衣服特别像你",
    "你今晚吃什么",
    "我论文改到第四版了，导师还是说不行",
    "算了先不管了，你那边下雨没",
    "周末要不要一起打游戏",
    "感觉最近有点悬，不知道能不能中",
    "我先去洗澡了",
]

ARMS = ["standard", "human"]

STRUCTURE_WORDS = ("首先", "其次", "总之", "不过话说回来", "综上", "总而言之")
FILLERS = ("嗯", "啊这", "算了", "绝了", "行吧", "哈哈", "救命", "笑死")


def _metrics(transcripts: list[list[str]]) -> dict[str, float]:
    bubbles = [text for turn in transcripts for text in turn]
    total_chars = sum(len(text) for text in bubbles)
    questions = sum(1 for text in bubbles if text.strip().endswith(("？", "?")))
    structure = sum(text.count(word) for text in bubbles for word in STRUCTURE_WORDS)
    fillers = sum(text.count(word) for text in bubbles for word in FILLERS)

    # 跨轮重复：6 字以上的完全相同片段
    seen: dict[str, int] = {}
    for text in bubbles:
        for chunk in re.split(r"[，。！？\s~…]+", text):
            if len(chunk) >= 6:
                seen[chunk] = seen.get(chunk, 0) + 1
    repeats = sum(count - 1 for count in seen.values() if count > 1)

    return {
        "气泡数": len(bubbles),
        "总字数": total_chars,
        "平均每条字数": round(total_chars / max(1, len(bubbles)), 1),
        "以问句结尾": questions,
        "结构词": structure,
        "口头语": fillers,
        "跨轮重复片段": repeats,
    }


async def run_arm(arm: str, settings, base: datetime, turns: list[str]):
    db_path = ROOT / "experiments" / "logs" / f"ab_{arm}.db"
    for suffix in ("", "-wal", "-shm"):
        Path(str(db_path) + suffix).unlink(missing_ok=True)

    local = dataclasses.replace(settings, db_path=db_path)
    db = Database(local.db_path)
    store = PersonaStore(local.persona_path)

    engine = ChatEngine(
        settings=local,
        provider=build_provider(local),
        db=db,
        persona_store=store,
        profile_store=ProfileStore(local.profile_path),
        lorebook=Lorebook.from_book(store.load().book),
        life=load_life(local.life_path),
        search=SearchClient(db, local.llm_base_url, local.llm_api_key, local.tz, enabled=False),
    )

    await engine.startup()
    await db.set_state(STATE_KEY, arm)  # standard 的包里没有任何块，等价于关闭

    transcripts: list[list[str]] = []
    for index, text in enumerate(turns):
        now = base + timedelta(minutes=2 * index)
        engine.clock = lambda now=now: now  # 同一时间线，两臂一致
        bubbles = await engine.handle(text)
        transcripts.append([bubble.text for bubble in bubbles])
        print(f"  [{arm}] {index + 1}/{len(turns)} {text[:12]}… → {len(bubbles)} 条", flush=True)

    engine.clock = lambda: base
    await engine.drain()
    db.close()
    return transcripts


def _render(results: dict[str, list[list[str]]], turns: list[str], model: str) -> str:
    lines = [f"# 风格包对照 · {now_local('Asia/Shanghai').strftime('%Y-%m-%d %H:%M')}", "", f"- 模型：{model}"]
    lines.append(f"- 轮数：{len(turns)}，两臂同一台词、同一时间线")

    lines += ["", "## 汇总", "", "| 指标 | " + " | ".join(ARMS) + " |", "|---|" + "---|" * len(ARMS)]
    metrics = {arm: _metrics(results[arm]) for arm in ARMS}
    for key in metrics[ARMS[0]]:
        row = " | ".join(str(metrics[arm][key]) for arm in ARMS)
        lines.append(f"| {key} | {row} |")

    lines += ["", "## 逐轮", ""]
    for index, text in enumerate(turns):
        lines.append(f"### {index + 1}. 他：{text}")
        for arm in ARMS:
            got = results[arm][index]
            lines.append(f"**{arm}**")
            lines.extend(f"- {bubble}" for bubble in got) if got else lines.append("- （没有回复）")
        lines.append("")
    return "\n".join(lines)


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--turns", type=int, default=len(TURNS))
    args = parser.parse_args()

    settings = load_settings()
    if settings.mock_mode:
        print("没有 LLM_API_KEY，跑不了真实对照。")
        return 1

    packs = PromptPacks(settings.prompts_path)
    print(f"风格包配置：{settings.prompts_path}")
    print(f"包：{[pack['id'] for pack in packs.catalog()]}")
    if "human" not in {pack["id"] for pack in packs.catalog()}:
        print("配置里没有 human 包。")
        return 1

    turns = TURNS[: args.turns]
    base = now_local(settings.tz).replace(hour=20, minute=0, second=0, microsecond=0)
    (ROOT / "experiments" / "logs").mkdir(parents=True, exist_ok=True)

    results = {}
    for arm in ARMS:
        print(f"\n== {arm} ==", flush=True)
        results[arm] = await run_arm(arm, settings, base, turns)

    markdown = _render(results, turns, settings.llm_model)
    out = ROOT / "experiments" / "logs" / f"ab_packs_{now_local(settings.tz).strftime('%Y%m%d-%H%M')}.md"
    out.write_text(markdown, encoding="utf-8")

    print("\n" + "=" * 60)
    print(markdown)
    print("=" * 60)
    print(f"\n已写入 {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
