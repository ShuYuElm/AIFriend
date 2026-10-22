#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""模型盲评：同一段人设、同一组问题，跑两个模型，并排输出。

用法：
    python experiments/ab_model.py
    python experiments/ab_model.py deepseek-flash deepseek-v4-pro

输出同时打印，并存到 experiments/logs/ab-<时间>.md，方便逐条比。
"""

from __future__ import annotations

import asyncio
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.config import load_settings  # noqa: E402
from app.llm import LLMError, OpenAICompatProvider  # noqa: E402
from app.persona import PersonaStore, RuntimeContext, build_p1, now_local  # noqa: E402

PROMPTS = [
    "今天好累啊",
    "我好像感冒了",
    "你在干嘛",
    "我下周要去面试",
    "睡了吗",
    "有点想你了",
]

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass


async def run_one(provider, system: str, text: str) -> tuple[str, float]:
    started = time.perf_counter()
    try:
        reply = await provider.chat(
            [{"role": "system", "content": system}, {"role": "user", "content": text}],
            temperature=0.9,
            max_tokens=120,
            thinking=False,
        )
    except LLMError as exc:
        reply = f"[失败] {exc}"
    return reply, time.perf_counter() - started


async def main() -> int:
    models = sys.argv[1:] or ["deepseek-flash", "deepseek-v4-pro"]
    settings = load_settings()
    if settings.mock_mode:
        print("没有 API Key")
        return 2

    card = PersonaStore(settings.persona_path).load()
    ctx = RuntimeContext(
        now=now_local(settings.tz),
        gap_seconds=3600 * 10,
        stage=card.stage,
        life_state="上课",
        life_detail="新闻传播专业课",
        days_known=93,
    )
    system = build_p1(card, ctx)

    providers = [
        (name, OpenAICompatProvider(settings.llm_base_url, settings.llm_api_key, name))
        for name in models
    ]

    lines = [f"# 模型盲评 {datetime.now():%Y-%m-%d %H:%M}", "", f"人设：{card.name}", ""]
    print()
    for prompt in PROMPTS:
        print(f"【{prompt}】")
        lines += [f"## {prompt}", ""]
        for name, provider in providers:
            reply, elapsed = await run_one(provider, system, prompt)
            print(f"  {name:<18} {elapsed:4.2f}s  {reply}")
            lines += [f"**{name}** ({elapsed:.2f}s)  ", f"> {reply}", ""]
        print()

    for _, provider in providers:
        await provider.aclose()

    log_dir = ROOT / "experiments" / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    path = log_dir / f"ab-{datetime.now():%Y%m%d-%H%M}.md"
    path.write_text("\n".join(lines), encoding="utf-8")
    print(f"已保存：{path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
