"""FastAPI 入口：HTTP + SSE。

启动时装配引擎和心跳，心跳常驻——这才是"她整天都活着"。
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, PlainTextResponse, StreamingResponse

from .adapters.web import EventBus
from . import __version__
from .chat import ChatEngine
from .config import ROOT, Settings, load_settings
from .db import Database
from .editing import (
    editable_to_persona,
    local_path,
    persona_to_editable,
    read_yaml,
    validate_persona_data,
    write_yaml,
)
from .heartbeat import Heartbeat
from .life import load_life
from .llm import build_provider
from .lorebook import Lorebook
from .models import Bubble
from .persona import PersonaStore, now_local, parse_persona
from .profile import ProfileStore
from .prompts import STATE_KEY as PROMPT_STATE_KEY

logger = logging.getLogger("aigirl.main")

WEB_DIR = ROOT / "web"
STATIC_FILES = {
    "app.js": "application/javascript",
    "settings.js": "application/javascript",
    "style.css": "text/css",
}


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or load_settings()
    bus = EventBus()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        db_existed = settings.db_path.exists()
        db = Database(settings.db_path)
        if db_existed:
            try:
                saved = await db.backup(
                    settings.db_path.parent / "backups", keep=7,
                    stamp=now_local(settings.tz).strftime("%Y%m%d-%H%M%S"),
                )
                logger.info("已备份数据库：%s", saved.name)
            except Exception as exc:
                logger.warning("备份失败：%s", exc)
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
        await engine.startup()

        async def deliver(bubbles: list[Bubble], kind: str) -> None:
            await bus.publish(
                "bubble",
                {
                    "kind": kind,
                    "bubbles": [{"text": b.text, "delay_ms": b.delay_ms} for b in bubbles],
                },
            )

        heartbeat = Heartbeat(
            settings=settings,
            db=db,
            provider=provider,
            persona_store=store,
            profile_store=profile,
            life=life,
            memory=engine.memory,
            experiences=engine.experiences,
            world=engine.world,
            on_message=deliver,
        )
        heartbeat.start()

        app.state.db = db
        app.state.provider = provider
        app.state.engine = engine
        app.state.heartbeat = heartbeat
        app.state.persona = persona
        app.state.bus = bus
        app.state.settings = settings
        logger.info("服务已启动：%s（%s）", persona.name, provider.model)

        try:
            yield
        finally:
            await heartbeat.stop()
            await engine.drain()
            await engine.world.close()
            db.close()
            if hasattr(provider, "aclose"):
                await provider.aclose()  # type: ignore[attr-defined]

    app = FastAPI(title="AIGirl", lifespan=lifespan)

    # ---------------------------------------------------------------- 页面

    @app.get("/")
    async def index() -> FileResponse:
        return FileResponse(WEB_DIR / "index.html")

    @app.get("/static/{name}")
    async def static(name: str) -> FileResponse:
        if name not in STATIC_FILES:
            raise HTTPException(status_code=404)
        return FileResponse(WEB_DIR / name, media_type=STATIC_FILES[name])

    # ---------------------------------------------------------------- API

    @app.post("/api/chat")
    async def chat(request: Request, payload: dict[str, Any]) -> dict[str, Any]:
        text = str(payload.get("text") or "").strip()
        if not text:
            return {"ok": False, "error": "empty"}
        bubbles = await request.app.state.engine.handle(text, channel="web")
        return {
            "ok": True,
            "bubbles": [{"text": b.text, "delay_ms": b.delay_ms} for b in bubbles],
        }

    @app.get("/api/history")
    async def history(request: Request, limit: int = 50) -> dict[str, Any]:
        messages = await request.app.state.db.recent_messages(max(1, min(limit, 200)))
        return {
            "messages": [
                {"role": m.role, "content": m.content, "created_at": m.created_at}
                for m in messages
            ]
        }

    @app.get("/api/state")
    async def state(request: Request) -> dict[str, Any]:
        db: Database = request.app.state.db
        # 每次现读：人设卡可能在编辑面板里刚改过，不能用启动时的快照
        persona = request.app.state.engine.persona_store.load()
        today = now_local(request.app.state.settings.tz).date().isoformat()
        memories = await db.list_memories(20)
        experiences = await db.experiences_for_day(today)
        heartbeats = await db.recent_heartbeats(10)
        return {
            "name": persona.name,
            "user_nickname": persona.user_nickname,
            "stage": await db.get_state("relationship_stage", persona.stage),
            "msg_count": await db.count_messages(),
            "proactive_today": await db.count_proactive(today),
            "memories": [
                {"id": m.id, "topic": m.topic or m.kind, "content": m.content, "old": m.is_old}
                for m in memories
            ],
            "experiences": [
                {"id": e["id"], "content": e["content"], "status": e["status"]} for e in experiences
            ],
            "heartbeats": heartbeats,
        }

    @app.get("/api/health")
    async def health(request: Request) -> dict[str, Any]:
        provider = request.app.state.provider
        return {
            "ok": True,
            "version": __version__,
            "model": provider.model,
            "provider": provider.name,
            "mock": getattr(provider, "name", "") == "mock",
            "sse_clients": request.app.state.bus.subscriber_count,
        }

    # ---------------------------------------------------------------- 配置编辑

    @app.get("/api/persona")
    async def get_persona(request: Request) -> dict[str, Any]:
        store = request.app.state.engine.persona_store
        card = store.load()
        raw = read_yaml(store.path)
        return {
            "ok": True,
            "persona": persona_to_editable(card, raw),
            "path": str(local_path(store.path)),
        }

    @app.put("/api/persona")
    async def put_persona(request: Request, payload: dict[str, Any]) -> dict[str, Any]:
        store = request.app.state.engine.persona_store
        target = local_path(store.path)
        base = read_yaml(target if target.exists() else store.path)
        data = editable_to_persona(payload, base)

        missing = validate_persona_data(data)
        if missing:
            return {"ok": False, "errors": [f"这些字段不能为空：{'、'.join(missing)}"]}

        try:
            parse_persona(data)  # 先验证结构，再落盘
            write_yaml(target, data)
            store.invalidate(target)
            card = store.load()
        except Exception as exc:  # 解析或写入失败都不该把服务搞崩
            logger.warning("保存人设卡失败：%s", exc)
            return {"ok": False, "errors": [f"保存失败：{exc}"]}

        return {"ok": True, "name": card.name, "path": str(target)}

    @app.get("/api/profile")
    async def get_profile(request: Request) -> dict[str, Any]:
        profile = request.app.state.engine.profile_store.load()
        return {"ok": True, "name": profile.name, "facts": profile.facts}

    @app.put("/api/profile")
    async def put_profile(request: Request, payload: dict[str, Any]) -> dict[str, Any]:
        facts = payload.get("facts")
        if not isinstance(facts, list):
            return {"ok": False, "errors": ["facts 必须是数组"]}

        store = request.app.state.engine.profile_store
        target = local_path(store.path)
        base = read_yaml(target if target.exists() else store.path)
        data = dict(base)
        data.pop("sections", None)  # 表单是扁平列表，分组信息不再保留
        data["facts"] = [str(f).strip() for f in facts if str(f).strip()]

        try:
            write_yaml(target, data)
            store.invalidate(target)
            profile = store.load()
        except Exception as exc:
            logger.warning("保存用户档案失败：%s", exc)
            return {"ok": False, "errors": [f"保存失败：{exc}"]}

        return {"ok": True, "count": len(profile.facts), "path": str(target)}

    # ---------------------------------------------------------------- 风格与情绪

    @app.get("/api/prompts")
    async def get_prompts(request: Request) -> dict[str, Any]:
        engine = request.app.state.engine
        active = str(await engine.db.get_state(PROMPT_STATE_KEY) or engine.packs.default_active())
        return {
            "ok": True,
            "active": active,
            "packs": engine.packs.catalog(),
            "text": engine.packs.render(active),
        }

    @app.put("/api/prompts")
    async def put_prompts(request: Request, payload: dict[str, Any]) -> dict[str, Any]:
        engine = request.app.state.engine
        known = {pack["id"] for pack in engine.packs.catalog()}
        active = str(payload.get("active") or "").strip()
        if active not in known:
            return {"ok": False, "errors": [f"没有这个风格包：{active}"]}

        await engine.db.set_state(PROMPT_STATE_KEY, active)
        return {"ok": True, "active": active, "text": engine.packs.render(active)}

    @app.get("/api/mood")
    async def get_mood(request: Request) -> dict[str, Any]:
        engine = request.app.state.engine
        mood = await engine.mood.load()
        return {
            "ok": True,
            "level": mood.level,
            "note": mood.note,
            "text": engine.mood.render(mood),
        }

    @app.get("/api/export")
    async def export_chat(request: Request) -> PlainTextResponse:
        """把全部对话导成 Markdown，便于备份或在别处翻看。"""
        db: Database = request.app.state.db
        settings: Settings = request.app.state.settings
        persona = request.app.state.engine.persona_store.load()
        rows = await db.recent_messages(100000, roles=("user", "assistant"))

        lines = [
            f"# 和 {persona.name} 的对话",
            "",
            f"> 导出时间：{now_local(settings.tz):%Y-%m-%d %H:%M}",
            "",
        ]
        current_day = ""
        for message in rows:
            day = str(message.created_at)[:10]
            if day != current_day:
                lines += ["", f"## {day}", ""]
                current_day = day
            who = "我" if message.role == "user" else persona.name
            lines.append(f"**{who}** {str(message.created_at)[11:16]}")
            lines.append("")
            lines.append(message.content)
            lines.append("")

        memories = await db.list_memories(200)
        if memories:
            lines += ["", "## 她记得的事", ""]
            lines += [f"- {item.content}" for item in memories]

        return PlainTextResponse(
            "\n".join(lines),
            media_type="text/markdown; charset=utf-8",
            headers={"Content-Disposition": 'attachment; filename="aigirl-chat.md"'},
        )

    @app.post("/api/heartbeat/tick")
    async def heartbeat_tick(request: Request) -> dict[str, Any]:
        """强制跳一次心跳（跳过闸门），调语气和演示用。"""
        return await request.app.state.heartbeat.tick(force=True)

    @app.get("/api/events")
    async def events(request: Request) -> StreamingResponse:
        queue = request.app.state.bus.subscribe()

        async def stream():
            try:
                yield ": connected\n\n"
                while True:
                    try:
                        event, data = await asyncio.wait_for(queue.get(), timeout=25)
                    except asyncio.TimeoutError:
                        yield ": ping\n\n"
                        continue
                    yield EventBus.format(event, data)
            finally:
                request.app.state.bus.unsubscribe(queue)

        return StreamingResponse(
            stream(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )

    return app


app = create_app()
