"""终端聊天入口：python -m app.cli"""

from __future__ import annotations

import asyncio
import logging
import sys

from .chat import ChatEngine
from .config import load_settings
from .db import Database
from .heartbeat import Heartbeat
from .life import load_life
from .llm import build_provider
from .lorebook import Lorebook
from .persona import PersonaStore, now_local
from .profile import ProfileStore

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stdin.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass


async def main() -> int:
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    settings = load_settings()

    db = Database(settings.db_path)
    provider = build_provider(settings)
    store = PersonaStore(settings.persona_path)
    profile_store = ProfileStore(settings.profile_path)
    life = load_life(settings.life_path)

    engine = None
    heartbeat = None

    try:
        persona = store.load()
        engine = ChatEngine(
            settings=settings,
            provider=provider,
            db=db,
            persona_store=store,
            profile_store=profile_store,
            lorebook=Lorebook.from_book(persona.book),
            life=life,
        )
        await engine.startup()

        async def deliver(bubbles, kind: str) -> None:
            print(f"\n  （{kind}）")
            for bubble in bubbles:
                await asyncio.sleep(bubble.delay_ms / 1000)
                print(f"{persona.name} > {bubble.text}")

        heartbeat = Heartbeat(
            settings=settings,
            db=db,
            provider=provider,
            persona_store=store,
            profile_store=profile_store,
            life=life,
            memory=engine.memory,
            experiences=engine.experiences,
            world=engine.world,
            on_message=deliver,
        )

        has_history = await db.count_messages() > 0
        now = now_local(settings.tz)
        print()
        print(f"  {persona.name} · CLI")
        print(f"  他叫 {persona.user_nickname}　｜　她此刻：{life.state_at(now).state or '—'}")
        print(f"  模型 {'离线 Mock（未配置 Key）' if settings.mock_mode else provider.model + '（' + provider.name + '）'}")
        if settings.mock_mode:
            print("  ! 没有 LLM_API_KEY，当前是 Mock 模式：回复由模板生成，只验证链路")
        print("  /quit 退出　/mem 记忆　/exp 今天的事　/sum 日记　/hb 心跳　/tick 强制跳一次" + ("" if has_history else "　（她先说了一句）"))
        print()

        if not has_history:
            print(f"{persona.name}：{persona.first_mes}")
            await db.insert_message("assistant", persona.first_mes, "cli", now.isoformat(), meta={"first_mes": True})

        heartbeat.start()

        while True:
            try:
                text = await asyncio.to_thread(input, "\n你 > ")
            except (EOFError, KeyboardInterrupt):
                print()
                break

            text = text.strip()
            if not text:
                continue
            if text in ("/quit", "/exit", "/q"):
                break
            if text.startswith("/search "):
                query = text[8:].strip()
                results = await engine.search.search(query) if query else []
                if not results:
                    print("  没搜到（或今天额度用完了）")
                for item in results:
                    print(f"  - {item['title']}: {item['summary'][:70]}")
                continue
            if text == "/mem":
                for item in reversed(await db.list_memories(8)):
                    mark = "（以前）" if item.superseded_by else ""
                    print(f"  [{item.id}] {item.topic or item.kind} {item.importance}★ {mark}{item.content}")
                continue
            if text == "/exp":
                day = now_local(settings.tz).date().isoformat()
                rows = await db.experiences_for_day(day)
                if not rows:
                    print("  今天还没有经历")
                for row in rows:
                    print(f"  [{row['id']}] {row['status']:<8} {str(row['occurred_at'])[11:16]}→{str(row['share_after'])[11:16]} {row['content']}")
                continue
            if text == "/sum":
                for row in reversed(await db.summaries(3)):
                    print(f"  {str(row['period_end'])[:10]}：{row['content']}")
                continue
            if text == "/summarize":
                done = await engine.summarize_day()
                print("  写好了" if done else "  没写（消息太少，或这天已经有日记）")
                continue
            if text == "/tick":
                result = await heartbeat.tick(force=True)
                print(f"  {result}")
                continue
            if text == "/hb":
                for row in await db.recent_heartbeats(10):
                    print(f"  {row['at'][11:16]} {row['action']:<8} {row['gate'] or '-':<10} {row['motivation']} {row['reason']}")
                continue

            bubbles = await engine.handle(text, channel="cli")
            print()
            for bubble in bubbles:
                await asyncio.sleep(bubble.delay_ms / 1000)
                print(f"{persona.name} > {bubble.text}")
    finally:
        if heartbeat is not None:
            await heartbeat.stop()
        if engine is not None:
            await engine.drain()
        if hasattr(provider, "aclose"):
            await provider.aclose()  # type: ignore[attr-defined]
        db.close()

    print()
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(asyncio.run(main()))
    except KeyboardInterrupt:
        raise SystemExit(0)
