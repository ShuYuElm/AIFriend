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
from fastapi.responses import FileResponse, StreamingResponse

from .adapters.web import EventBus
from . import __version__
from .chat import ChatEngine
from .config import ROOT, Settings, load_settings
from .db import Database
from .heartbeat import Heartbeat
from .life import load_life
from .llm import build_provider
from .lorebook import Lorebook
from .models import Bubble
from .persona import PersonaStore, now_local
from .profile import ProfileStore

logger = logging.getLogger("aigirl.main")

WEB_DIR = ROOT / "web"
STATIC_FILES = {"app.js": "application/javascript", "style.css": "text/css"}


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or load_settings()
    bus = EventBus()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
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
        persona = request.app.state.persona
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
